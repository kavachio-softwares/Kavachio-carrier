"""
A treaty's reinsurance bordereau, written where the rest of Kavachio's data
lives — so validation and results can be read the same way as a broker's.

Kavachio's reinsurance tables (ceded_setup, ceded_bordereau, ceded_statement,
ceded_decision) stay the working copy every reinsurance screen runs on. This
writes the same facts, as they happen, to the shared places:

  rules       validation_rule        one row per treaty rule, marked
                                     rule_spec.source = 'reinsurance' and found
                                     again by rule_spec.treaty_rule_id — updated
                                     in place, so its rule_id never changes
  checks      check_run / check_result / check_error (check_record), lane
                                     'reinsurance': each statement prepared,
                                     every rule it ran, every row it caught
  decisions   exception_decision_log, lane 'reinsurance'
  the treaty  reinsurance_arrangement   one row per treaty, kept current
  amounts     cession                one row per statement and reinsurer

The broker side never sees these rows: its rule readers and deleters skip
rule_spec.source = 'reinsurance', its reports count the direct and canonical
lanes only, and the audit feed leaves the 'reinsurance' decision lane to the
request log that already records it.

Nothing here ever stops the work it records: every write runs in a savepoint,
and a failure is logged and dropped. Columns that a database may not have yet
(migration 46, or a database built from the code's model) are written only
where they exist.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Iterable, Optional

from sqlalchemy import text

import check_record

log = logging.getLogger("kavachio.ceded.canonical")

REINSURANCE = check_record.REINSURANCE      # rule_spec.source, check lane, decision lane

# The data model's arrangement types. Surplus and stop loss have none — the
# treaty's own type is kept in arrangement_terms either way.
ARRANGEMENT_TYPE = {
    "quota_share": "treaty_quota_share",
    "xol": "treaty_excess_of_loss",
    "facultative": "facultative_individual",
}

_cols_cache: dict[str, set[str]] = {}


def _cols(s, table: str) -> set[str]:
    """The columns `table` has on this database (read once per process)."""
    if table not in _cols_cache:
        rows = s.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :t AND table_schema = ANY(current_schemas(false))"),
            {"t": table}).all()
        _cols_cache[table] = {r[0] for r in rows}
    return _cols_cache[table]


def _safe(what: str, s, fn, *args, **kw):
    """Run `fn` in a savepoint; never let it cost the caller."""
    try:
        with s.begin_nested():
            return fn(s, *args, **kw)
    except Exception:  # noqa: BLE001 — the copy never costs the work
        log.warning("reinsurance %s not written to the data model", what, exc_info=True)
        return None


def _j(v: Any) -> str:
    return json.dumps(v, default=str)


# ── rules → validation_rule ─────────────────────────────────────────────────
def _write_rules(s, c, rules: list[dict]) -> dict[str, int]:
    import treaty_rules as trr
    have = {r[1]: r[0] for r in s.execute(text(
        "SELECT rule_id, rule_spec->>'treaty_rule_id' FROM validation_rule "
        "WHERE contract_id = :c AND rule_spec->>'source' = :src"),
        {"c": c.id, "src": REINSURANCE}).all()}
    out: dict[str, int] = {}
    for r in rules or []:
        rid = str(r.get("id") or "")
        if not rid:
            continue
        fields = trr.SUBJECT_FIELDS.get(r.get("subject"), ())
        row = {
            "tenant_id": c.tenant_id, "contract_id": c.id, "program_id": c.program_id,
            "rule_name": str(r.get("label") or rid)[:500],
            "rule_description": r.get("clause") or r.get("label"),
            "severity": r.get("severity") if r.get("severity") in ("critical", "warning")
            else "warning",
            "canonical_target": _j({"canonical_field": fields[0] if fields else None,
                                    "fields": list(fields), "subject": r.get("subject")}),
            "rule_spec": _j({"source": REINSURANCE, "treaty_rule_id": rid,
                             "kind": r.get("kind"), "subject": r.get("subject"),
                             "params": r.get("params") or {}, "origin": r.get("source"),
                             "clause": r.get("clause"), "endorsement": r.get("endorsement"),
                             "effective_from": r.get("effective_from")}),
            "error_message": str(r.get("label") or rid)[:500],
            "status": "active" if r.get("enabled", True) else "disabled",
            "effective_from": r.get("effective_from"),
            "verbatim": r.get("clause"),
        }
        if rid in have:
            s.execute(text(
                "UPDATE validation_rule SET program_id = :program_id, "
                "rule_name = :rule_name, rule_description = :rule_description, "
                "severity = :severity, canonical_target = CAST(:canonical_target AS JSONB), "
                "rule_spec = CAST(:rule_spec AS JSONB), error_message = :error_message, "
                "rule_status = :status, "
                "effective_from = COALESCE(CAST(:effective_from AS DATE), effective_from), "
                "source_verbatim_text = :verbatim, updated_at = now(), "
                "updated_by = 'reinsurance' WHERE rule_id = :id"), {**row, "id": have[rid]})
            out[rid] = have[rid]
        else:
            out[rid] = s.execute(text(
                "INSERT INTO validation_rule (tenant_id, contract_id, program_id, "
                " rule_engine, rule_name, rule_description, validation_stage, severity, "
                " canonical_target, rule_spec, error_message, generation_confidence, "
                " rule_status, effective_from, source_verbatim_text, created_by, updated_by) "
                "VALUES (:tenant_id, :contract_id, :program_id, 'custom', :rule_name, "
                " :rule_description, 'input', :severity, CAST(:canonical_target AS JSONB), "
                " CAST(:rule_spec AS JSONB), :error_message, 1.0, :status, "
                " COALESCE(CAST(:effective_from AS DATE), CURRENT_DATE), :verbatim, "
                " 'reinsurance', 'reinsurance') RETURNING rule_id"), row).scalar()
    # A rule the treaty no longer has is switched off, not deleted: a check
    # already recorded against it still names it.
    gone = [v for k, v in have.items() if k not in out]
    if gone:
        s.execute(text("UPDATE validation_rule SET rule_status = 'disabled', "
                       "updated_at = now(), updated_by = 'reinsurance' "
                       "WHERE rule_id = ANY(:ids)"), {"ids": gone})
    return out


def sync_rules(s, c, rules: list[dict]) -> dict[str, int]:
    """The treaty's rules in validation_rule. {treaty rule id: rule_id}."""
    return _safe("rules", s, _write_rules, c, rules) or {}


def rule_ids(s, c) -> dict[str, int]:
    """{treaty rule id: rule_id} as written last."""
    try:
        return {r[1]: r[0] for r in s.execute(text(
            "SELECT rule_id, rule_spec->>'treaty_rule_id' FROM validation_rule "
            "WHERE contract_id = :c AND rule_spec->>'source' = :src"),
            {"c": c.id, "src": REINSURANCE}).all()}
    except Exception:  # noqa: BLE001
        return {}


# ── a statement's check → check_run / check_result / check_error ────────────
def check_parts(rules: list[dict], checks: list[dict],
                ids: dict[str, int]) -> tuple[list[dict], list[dict]]:
    """(results, errors) as check_record takes them: every enabled rule —
    passed, failed (with how many rows) or not run (no row carries what it
    reads) — and every row a rule caught."""
    by_id = {x.get("id"): x for x in checks or []}
    results, errors = [], []
    for r in rules or []:
        if not r.get("enabled", True):
            continue
        key = f"treaty_rule:{r['id']}"
        x = by_id.get(r["id"])
        row = {"check_key": key, "kind": "treaty_rule", "rule_id": ids.get(r["id"]),
               "name": str(r.get("label") or r["id"])[:250], "severity": r.get("severity")}
        if x and x.get("severity") == "untested":
            row.update(outcome=check_record.NOT_RUN, error_count=0, message=x.get("why"))
        elif x and x.get("rows"):
            row.update(outcome=check_record.FAILED, error_count=int(x["rows"]))
        else:
            row.update(outcome=check_record.PASSED, error_count=0)
        results.append(row)
    for x in checks or []:
        for it in x.get("items") or []:
            errors.append({
                "check_key": f"treaty_rule:{x['id']}", "rule_id": ids.get(x["id"]),
                "severity": x.get("severity"), "field": x.get("field"),
                "policy_number": it.get("policy"), "actual_value": it.get("value"),
                "row": (it["row"] + 1) if isinstance(it.get("row"), int) else None,
                "message": f"{x.get('label')}: {it.get('why')}"})
    return results, errors


def record_check(s, c, cb, rules: list[dict], checks: list[dict], *,
                 rows_checked: Optional[int], refresh: bool,
                 user_id: Optional[int], ids: dict[str, int]) -> Optional[int]:
    """One prepared statement in the shared check record."""
    results, errors = check_parts(rules, checks, ids)
    return check_record.record_run(
        lane=REINSURANCE,
        kind=check_record.RERUN if refresh else check_record.RUN,
        tenant_id=c.tenant_id, status="has_exceptions" if errors else "clean",
        results=results, exceptions=errors,
        dv={"stats": {"rows_validated": rows_checked}},
        program_id=c.program_id, contract_id=c.id,
        filename=f"{c.name or 'Treaty'} — {cb.period}", period=cb.period,
        chain_key=f"ceded:{c.id}:{cb.period}", submission_ref=f"ceded_bordereau:{cb.id}",
        version_no=cb.version_no, run_by_user_id=user_id, session=s)


# ── a decision → exception_decision_log ─────────────────────────────────────
def log_decision(s, who: dict, c, chk: dict, item: dict, *, kind: str,
                 new_value: Optional[str], reason: Optional[str],
                 ids: dict[str, int]) -> None:
    def _write(s):
        import decision_log
        decision_log.record(
            s, who, lane=REINSURANCE, kind=kind, tenant_id=c.tenant_id,
            program_id=c.program_id, rule_id=ids.get(chk.get("id")),
            policy_number=item.get("policy"), field=chk.get("field"),
            row=(item["row"] + 1) if isinstance(item.get("row"), int) else None,
            old_value=item.get("value"), new_value=new_value, reason=reason)
        s.flush()
    _safe("decision", s, _write)


# ── the treaty → reinsurance_arrangement ────────────────────────────────────
def _write_arrangement(s, c) -> Optional[int]:
    import treaty_routes
    import treaty_types as tt
    st = treaty_routes.load_structure(s, c.id, names=False) or {}
    if not st.get("treaty_type"):
        return None
    cols = _cols(s, "reinsurance_arrangement")
    terms = st.get("terms") or {}
    layers = st.get("layers") or []
    first = (layers[0].get("terms") if layers else None) or {}
    ref = (terms.get("placement_reference") or "").strip() or None
    key = f"treaty:{c.id}"
    state = treaty_routes._state(c)
    vals: dict[str, Any] = {
        "tenant_id": c.tenant_id,
        "arrangement_type": ARRANGEMENT_TYPE.get(st["treaty_type"]),
        "arrangement_program_id": c.program_id,
        "arrangement_reference": ref,
        "arrangement_basis": terms.get("basis"),
        "arrangement_retention_amount": tt._num(first.get("retention")
                                                or terms.get("retention_line")),
        "arrangement_limit_amount": tt._num(first.get("limit") or terms.get("treaty_limit")),
        "arrangement_is_authorized": state not in treaty_routes.EDITABLE_STATES,
        "arrangement_terms": _j({"treaty_type": st["treaty_type"], "terms": terms,
                                 "layers": layers, "state": state}),
        "contract_id": c.id,
        "inception_dt": c.inception_dt, "expiry_dt": c.expiry_dt,
        "row_key": key, "modified_at": datetime.utcnow(),
    }
    # UMR is unique per carrier: set only while no other arrangement holds it.
    if "umr" in cols and ref:
        taken = s.execute(text(
            "SELECT 1 FROM reinsurance_arrangement WHERE tenant_id = :t AND umr = :u "
            "AND COALESCE(row_key, '') <> :k"), {"t": c.tenant_id, "u": ref, "k": key}).first()
        vals["umr"] = None if taken else ref
    vals = {k: v for k, v in vals.items() if k in cols}
    found = s.execute(text("SELECT arrangement_id FROM reinsurance_arrangement "
                           "WHERE row_key = :k"), {"k": key}).scalar()
    typ = "CAST(:arrangement_type AS arrangement_type_e)" if _enum_type(s) \
        else ":arrangement_type"
    expr = {k: (typ if k == "arrangement_type" else f":{k}") for k in vals}
    if found:
        s.execute(text("UPDATE reinsurance_arrangement SET "
                       + ", ".join(f"{k} = {expr[k]}" for k in vals)
                       + " WHERE arrangement_id = :id"), {**vals, "id": found})
        return found
    for k in ("created_at", "valid_from"):
        if k in cols:
            vals[k] = datetime.utcnow()
            expr[k] = f":{k}"
    if "is_current_version" in cols:
        vals["is_current_version"], expr["is_current_version"] = True, ":is_current_version"
    return s.execute(text(
        "INSERT INTO reinsurance_arrangement (" + ", ".join(vals) + ") VALUES ("
        + ", ".join(expr[k] for k in vals) + ") RETURNING arrangement_id"), vals).scalar()


def _enum_type(s) -> bool:
    if "_enum" not in _cols_cache:
        _cols_cache["_enum"] = {"yes"} if s.execute(text(
            "SELECT 1 FROM pg_type WHERE typname = 'arrangement_type_e'")).first() else set()
    return bool(_cols_cache["_enum"])


def sync_arrangement(s, c) -> Optional[int]:
    """The treaty as the data model's reinsurance arrangement. Its id."""
    return _safe("arrangement", s, _write_arrangement, c)


# ── amounts → cession ───────────────────────────────────────────────────────
def _share(shares: Optional[dict]) -> Optional[float]:
    vals = {float(v) for v in (shares or {}).values() if v is not None}
    return vals.pop() if len(vals) == 1 else None


def _write_cessions(s, c, cb) -> int:
    import ceded_engine as ce
    from db import CededStatement
    arr = _write_arrangement(s, c)
    if arr is None:
        return 0
    cols = _cols(s, "cession")
    bounds = ce.period_bounds(cb.period)
    current = cb.status != "superseded"
    now = datetime.utcnow()
    wrote = 0
    keys = []
    for x in s.query(CededStatement).filter(CededStatement.cb_id == cb.id):
        t = x.totals or {}
        loss = [t.get(k) for k in ("claims_paid", "claims_outstanding") if t.get(k) is not None]
        key = f"ceded:{cb.id}:{x.reinsurer_party_id}"
        keys.append(key)
        vals: dict[str, Any] = {
            "tenant_id": c.tenant_id, "cession_arrangement_id": arr,
            "cession_reinsurer_party_id": x.reinsurer_party_id,
            "cession_share_percent": _share(x.shares),
            "cession_period_start": bounds[0].isoformat() if bounds else cb.period,
            "cession_period_end": bounds[1].isoformat() if bounds else cb.period,
            "cession_ceded_premium_amount": t.get("ceded_premium"),
            "cession_ceded_loss_amount": round(sum(loss), 2) if loss else None,
            "cession_recoveries_due_amount": t.get("recovery"),
            # Migration 46: written once the columns are there.
            "cession_ceding_commission_amount": t.get("ceding_commission"),
            "cession_net_premium_amount": t.get("net_premium"),
            "cession_status": "superseded" if not current else x.status,
            "is_current_version": current, "version_no": cb.version_no,
            "row_key": key, "modified_at": now,
            "valid_until": None if current else now,
        }
        vals = {k: v for k, v in vals.items() if k in cols}
        found = s.execute(text("SELECT cession_id FROM cession WHERE row_key = :k"),
                          {"k": key}).scalar()
        if found:
            s.execute(text("UPDATE cession SET " + ", ".join(f"{k} = :{k}" for k in vals)
                           + " WHERE cession_id = :id"), {**vals, "id": found})
        else:
            for k in ("created_at", "valid_from"):
                if k in cols:
                    vals[k] = now
            s.execute(text("INSERT INTO cession (" + ", ".join(vals) + ") VALUES ("
                           + ", ".join(f":{k}" for k in vals) + ")"), vals)
        wrote += 1
    # A reinsurer no longer on this statement (the panel changed before a
    # draft was prepared again): its row for this statement goes.
    s.execute(text("DELETE FROM cession WHERE row_key LIKE :p AND NOT (row_key = ANY(:k))"),
              {"p": f"ceded:{cb.id}:%", "k": keys})
    return wrote


def sync_cessions(s, c, cb) -> int:
    """Each reinsurer's amounts on `cb` as the data model's cession rows."""
    return _safe("cessions", s, _write_cessions, c, cb) or 0


def sync_statement(s, c, cb, *, rules: list[dict], checks: list[dict],
                   rows_checked: Optional[int], refresh: bool,
                   user_id: Optional[int]) -> None:
    """Everything a prepared statement adds: its rules, its check, its amounts."""
    ids = sync_rules(s, c, rules)
    record_check(s, c, cb, rules, checks, rows_checked=rows_checked, refresh=refresh,
                 user_id=user_id, ids=ids)
    sync_cessions(s, c, cb)


def written(s, c, cb_ids: Iterable[int]) -> dict:
    """What is in the data model for these statements — for tests and support."""
    keys = [f"ceded:{i}:%" for i in cb_ids]
    out = {"rules": len(rule_ids(s, c)), "cessions": []}
    for k in keys:
        out["cessions"] += [dict(r._mapping) for r in s.execute(
            text("SELECT * FROM cession WHERE row_key LIKE :k ORDER BY row_key"), {"k": k})]
    return out
