"""
rule_history.py
───────────────
Task 48.2 — the full history of every check: created, changed, switched off,
removed — with the earlier version kept rather than deleted or overwritten,
and the previous mapping kept on file. The Audit Logs "Rule History" tab reads
it back (read side at the bottom).

THE SAME ROWS AS CHANGE HISTORY
  Everything lands as activity_events "record_changed" (change_history.py),
  with a `rule` block naming the rule, where it lives, its version and its
  status after the change. Rule History is those rows, one line per event.

WHERE EACH KIND COMES FROM
  Rule Library rules, Programme Rules,  through the ORM — change_history's
  output template fields, mappings      session hook already sees them.
  A contract's own checks               validation_rule has no ORM model and is
                                        written in raw SQL, so:
    re-saving the contract's rules        contract_rules.write_rules calls
                                          snapshot_contract() before it deletes
                                          and record_replace() after it inserts:
                                          a rule whose settings did not change
                                          is NOT recorded, a changed one is one
                                          "Changed" with the old settings kept,
                                          a dropped one "Removed", a new one
                                          "Created".
    every other edit (tolerance,          one engine hook: an
    retarget, remove, re-date)            "UPDATE validation_rule … WHERE
                                          rule_id = :x" (or ANY(:ids)) is read
                                          before and after it runs, on the same
                                          connection, and the difference is
                                          written in the same transaction.

SAFETY
  Every read and write here runs inside a SAVEPOINT and swallows its own
  errors: a history that cannot be written must never break the save it
  describes, nor leave the save's transaction aborted.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from typing import Any, Optional

from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.sql.elements import TextClause

import change_history as ch

log = logging.getLogger("bdx.rule_history")

_COLS = ("rule_id, tenant_id, contract_id, program_id, rule_name, severity, rule_status, "
         "rule_spec, error_message, canonical_target, output_template_id")
_BUSY = "_kavachio_rule_history_busy"
_PENDING = "_kavachio_rule_history_pending"
STATUS_WORDS = {"active": "Active", "disabled": "Switched off", "needs_review": "Waiting for review",
                "removed": "Removed"}
# The compiled query is a cache of the rule, not the rule; the IR repeats the
# params when both are stored. Neither is something a person compares.
_SPEC_SKIP = ("compiled_sql", "sql", "duckdb_sql", "cached_sql", "output_template_id", "source")


# ── one contract check, as a person reads it ───────────────────────────────
def _json(v):
    if isinstance(v, str) and v.strip()[:1] in ("{", "["):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _settings(spec) -> Any:
    spec = _json(spec)
    if not isinstance(spec, dict):
        return spec
    out = {k: v for k, v in spec.items() if k not in _SPEC_SKIP}
    if "params" in out and "ir" in out:
        out.pop("ir")
    return out


def _field_of(target) -> Optional[str]:
    t = _json(target)
    return (t.get("output_field") or t.get("canonical_field")) if isinstance(t, dict) else None


def view(r: dict) -> dict:
    """The parts of a validation_rule row worth comparing."""
    return {"Rule name": r.get("rule_name"), "Field checked": _field_of(r.get("canonical_target")),
            "How strict": r.get("severity"), "Status": STATUS_WORDS.get(r.get("rule_status") or "",
                                                                        r.get("rule_status")),
            "Message": r.get("error_message"), "Settings": _settings(r.get("rule_spec"))}


def key_of(r: dict) -> str:
    return f"contract_rule:{r.get('contract_id')}:{r.get('rule_name') or r.get('rule_id')}:" \
           f"{_field_of(r.get('canonical_target')) or ''}"


def diff(before: Optional[dict], after: Optional[dict]) -> list[dict]:
    a, b = view(before or {}), view(after or {})
    out = []
    for label in a:
        if label == "Settings":
            out.extend(ch.json_changes("Settings", a[label], b[label]))
            continue
        o, n = ch.show(a[label]), ch.show(b[label])
        if o != n:
            out.append({"field": label, "old": o, "new": n})
    return out


def _event(before: Optional[dict], after: Optional[dict]) -> str:
    if before is None:
        return "created"
    if after is None:
        return "removed"
    if (before.get("rule_status") or "") != "disabled" and after.get("rule_status") == "disabled":
        return "switched_off"
    if before.get("rule_status") == "disabled" and (after.get("rule_status") or "") == "active":
        return "switched_on"
    return "changed"


def _history_row(conn, before: Optional[dict], after: Optional[dict], reason: Optional[str],
                 cause: Optional[str] = None) -> dict:
    r = after or before or {}
    ev = _event(before, after)
    changes = [] if ev in ("created", "removed") else diff(before, after)
    where = _contract_name(conn, r.get("contract_id"))
    status = "Removed" if after is None else STATUS_WORDS.get(after.get("rule_status") or "", after.get("rule_status"))
    details = {
        "entity": "contract_rule", "entity_id": r.get("rule_id"), "name": ch.show(r.get("rule_name"), 200),
        "module": "Contract rules", "kind": "Contract rule", "event": ev, "changes": changes,
        "more": 0, "reason": reason, "program_id": r.get("program_id"), "contract_id": r.get("contract_id"),
        "rule": {"key": key_of(r), "name": r.get("rule_name") or "Rule", "kind": "Contract rule",
                 "status": status, "where": where},
    }
    if before is not None and ev != "created":
        # The earlier version, kept whole — the row itself is overwritten or deleted.
        details["before"] = {"rule": {k: v for k, v in view(before).items()}}
    if cause:
        details["cause"] = cause
    return {"tenant_id": r.get("tenant_id"), "target": key_of(r), "details": details}


def _contract_name(conn, cid) -> Optional[str]:
    """Through the ORM's table, which knows the physical (prefixed) names."""
    if not cid:
        return None
    from sqlalchemy import select
    from db import Contract
    col = lambda attr: getattr(Contract, attr).property.columns[0]  # noqa: E731
    rows = _select(conn, select(col("name").label("name"), col("filename").label("filename"))
                   .where(col("id") == cid), {}, quiet=True)
    if rows:
        return rows[0].get("name") or rows[0].get("filename") or f"Contract #{cid}"
    return f"Contract #{cid}"


# ── reading and writing on the caller's own connection ─────────────────────
def _select(conn, sql, params: dict, quiet: bool = False) -> Optional[list[dict]]:
    """Rows as dicts, inside a SAVEPOINT; None if the read failed."""
    conn.info[_BUSY] = True
    try:
        with conn.begin_nested():
            stmt = text(sql) if isinstance(sql, str) else sql
            return [dict(m) for m in conn.execute(stmt, params).mappings().all()]
    except Exception as e:  # noqa: BLE001
        if not quiet:
            log.warning("rule_history: read failed: %s", e)
        return None
    finally:
        conn.info[_BUSY] = False


def _rules(conn, where: str, params: dict) -> Optional[dict[int, dict]]:
    rows = _select(conn, f"SELECT {_COLS} FROM validation_rule WHERE {where}", params)
    return None if rows is None else {r["rule_id"]: r for r in rows}


def _insert(conn, rows: list[dict]) -> None:
    if not rows:
        return
    ctx = ch.current() or {}
    uid = ctx.get("user_id")
    try:
        import audit
        from db import ActivityEvent
        email, role, broker_id = audit.actor_identity(uid)
        now = dt.datetime.utcnow()
        values = [{"tenant_id": r["tenant_id"] or ctx.get("tenant_id"), "actor": email, "action": ch.ACTION,
                   "target": r["target"], "details": r["details"], "created_at": now,
                   "actor_user_id": uid, "actor_role": role, "actor_broker_party_id": broker_id}
                  for r in rows]
    except Exception as e:  # noqa: BLE001
        log.warning("rule_history: could not prepare %s row(s): %s", len(rows), e)
        return
    conn.info[_BUSY] = True
    try:
        with conn.begin_nested():
            conn.execute(ActivityEvent.__table__.insert(), values)
    except Exception as e:  # noqa: BLE001
        log.warning("rule_history: could not record %s change(s): %s", len(rows), e)
    finally:
        conn.info[_BUSY] = False


def _recording() -> Optional[dict]:
    ctx = ch.current()
    return ctx if ctx and ctx.get("user_id") else None


# ── re-saving a contract's rules (contract_rules.write_rules) ──────────────
def snapshot_contract(conn, where: str, params: dict) -> Optional[dict]:
    """The rules write_rules is about to replace — the same WHERE it deletes by."""
    if not _recording():
        return None
    return _rules(conn, where, params)


def record_replace(conn, before: Optional[dict], where: str, params: dict) -> None:
    """After the new rules are in: what changed, by rule name and field.
    A rule written again exactly as it was is not a change and is not recorded."""
    ctx = _recording()
    if before is None or ctx is None:
        return
    after = _rules(conn, where, params)
    if after is None:
        return

    def by_key(rows: dict) -> dict:
        out: dict = {}
        for r in sorted(rows.values(), key=lambda x: x["rule_id"]):
            k = key_of(r)
            while k in out:
                k += "+"
            out[k] = r
        return out

    a, b = by_key(before), by_key(after)
    rows = []
    for k in sorted(set(a) | set(b)):
        o, n = a.get(k), b.get(k)
        if o is not None and n is not None and not diff(o, n):
            continue
        rows.append(_history_row(conn, o, n, ctx.get("reason"), cause="The contract's rules were saved again"))
    _insert(conn, rows)


# ── any other "UPDATE validation_rule … WHERE rule_id = …" ──────────────────
_UPDATE = re.compile(r"^\s*UPDATE\s+validation_rule\s+SET\s.+?\sWHERE\s+rule_id\s*=\s*"
                     r"(?:ANY\s*\(\s*:(?P<many>\w+)\s*\)|:(?P<one>\w+))", re.I | re.S)


def _match(conn, elem, params):
    if conn.info.get(_BUSY) or not isinstance(elem, TextClause):
        return None
    sql = elem.text
    if "validation_rule" not in sql[:120]:
        return None
    m = _UPDATE.match(sql)
    if not m or not isinstance(params, dict):
        return None
    name = m.group("many") or m.group("one")
    ids = params.get(name)
    if ids is None:
        return None
    ids = list(ids) if isinstance(ids, (list, tuple, set)) else [ids]
    return [int(i) for i in ids if i is not None][:500] or None


def _before_execute(conn, elem, multiparams, params, execution_options):
    try:
        if multiparams or not _recording():
            return
        ids = _match(conn, elem, params)
        if not ids:
            return
        before = _rules(conn, "rule_id = ANY(:ids)", {"ids": ids})
        if before is not None:
            # One at a time: a connection runs its statements in turn. The
            # statement object itself is kept, and matched by identity after,
            # so an UPDATE that failed can never be paired with a later one.
            conn.info[_PENDING] = (elem, ids, before)
    except Exception as e:  # noqa: BLE001
        log.warning("rule_history: before-read failed: %s", e)


def _after_execute(conn, elem, multiparams, params, execution_options, result):
    pending = conn.info.get(_PENDING)
    if not pending or pending[0] is not elem:
        return
    conn.info.pop(_PENDING, None)
    _elem, ids, before = pending
    try:
        ctx = _recording()
        after = _rules(conn, "rule_id = ANY(:ids)", {"ids": ids}) if ctx else None
        if after is None:
            return
        rows = [_history_row(conn, b, after.get(rid), ctx.get("reason"))
                for rid, b in before.items() if after.get(rid) is None or diff(b, after.get(rid))]
        _insert(conn, rows)
    except Exception as e:  # noqa: BLE001
        log.warning("rule_history: after-read failed: %s", e)


_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    event.listen(Engine, "before_execute", _before_execute)
    event.listen(Engine, "after_execute", _after_execute)
    event.listen(Engine, "rollback", lambda conn: conn.info.pop(_PENDING, None))
    _installed = True


# ═══════════════════════════════════════════════════════════════════════════
# READ SIDE — the Rule History tab
# ═══════════════════════════════════════════════════════════════════════════
KINDS = ("Contract rule", "Rule Library", "Programme rule", "Output fields", "Mapping")
_MAPPING_ENTITIES = ("setup_mapping", "mapping")


def _rows(s, principal, f, *, programme=None, kind=None):
    """Every rule / mapping event this viewer may read, OLDEST first (versions
    are counted forwards), each with its version and the one before it."""
    from sqlalchemy import cast, or_
    from sqlalchemy.dialects.postgresql import JSONB
    import audit_feed as af
    from db import ActivityEvent
    v = af.viewer_for(s, principal)
    sc = af.scope_for(s, v)
    # Versions are counted over a rule's WHOLE history, so the date filter is
    # applied after counting, not in SQL.
    whole = af.Filters(actor_user_ids=f.actor_user_ids, actor_broker_ids=f.actor_broker_ids,
                       actor_system=f.actor_system)
    q = af._base_query(s, ActivityEvent, sc, whole, ([], []))
    if q is None:
        return v, []
    d = cast(ActivityEvent.details, JSONB)
    q = q.filter(ActivityEvent.action == ch.ACTION,
                 or_(d.has_key("rule"), d["entity"].astext.in_(_MAPPING_ENTITIES)))
    if programme:
        q = q.filter(d["program_id"].astext == str(programme))
    rows = q.order_by(ActivityEvent.created_at.asc(), ActivityEvent.id.asc()).limit(ch.MAX_ROWS).all()
    out, seen = [], {}
    for e in rows:
        det = e.details if isinstance(e.details, dict) else {}
        rule = det.get("rule") or {"key": e.target, "name": det.get("name") or "Mapping", "kind": "Mapping",
                                   "status": "Kept on file", "where": None}
        key = rule.get("key") or e.target
        count = seen.get(key)
        if count is None:
            # A rule's first recorded event is version 1 when it was created
            # here; one edited before history began starts at version 2 — 1
            # being the version that was in place, kept in this row's `before`.
            count = 0 if det.get("event") == "created" else 1
        seen[key] = count + 1
        own = rule.get("version")          # Programme rules and templates number their own
        version = own if isinstance(own, int) else count + 1
        prev = None if det.get("event") == "created" else (version - 1 if version > 1 else None)
        out.append((e, det, rule, key, version, prev))
    if kind:
        out = [x for x in out if x[2].get("kind") == kind]
    return v, out


def _in_dates(e, f) -> bool:
    if f.since is not None and e.created_at < f.since:
        return False
    return not (f.until is not None and e.created_at > f.until)


def _matches(det, rule, q: str) -> bool:
    if not q:
        return True
    q = q.lower()
    return any(q in (str(x) or "").lower() for x in (rule.get("name"), rule.get("where"), det.get("name")))


def rules(s, principal, f, *, programme=None, kind=None, status=None, page=1, page_size=50) -> dict:
    import audit_history as ah
    v, rows = _rows(s, principal, f, programme=programme, kind=kind)
    people = ah.People(s, v)
    latest = {}
    for e, det, rule, key, version, prev in rows:
        latest[key] = (rule.get("status"), version)
    items = []
    for e, det, rule, key, version, prev in reversed(rows):
        if not _in_dates(e, f) or not _matches(det, rule, f.q):
            continue
        now_status = latest[key][0]
        if status and now_status != status:
            continue
        items.append({"event_id": e.id, "at": ch._iso(e.created_at), "key": key, "name": rule.get("name"),
                      "kind": rule.get("kind"), "where": rule.get("where") or (
                          "Rule Library" if rule.get("kind") == "Rule Library" else "—"),
                      "version": version, "previous": prev, "latest_version": latest[key][1],
                      "event": ch.EVENT_WORDS.get(det.get("event"), det.get("event")),
                      "status": now_status, "changes": len(det.get("changes") or []),
                      "reason": det.get("reason"), "by": people.name(e.actor_user_id, "Kavachio · Automatic")})
    keys = {i["key"] for i in items}
    counts = {"rules": len(keys), "events": len(items),
              "created": sum(1 for i in items if i["event"] == "Created"),
              "changed": sum(1 for i in items if i["event"] == "Changed"),
              "off": sum(1 for i in items if i["event"] in ("Switched off", "Removed", "Deleted", "Retired")),
              "mappings": len({i["key"] for i in items if i["kind"] == "Mapping"})}
    out = ah._page(items, page, page_size)
    out["counts"] = counts
    out["choices"] = {"kinds": list(KINDS),
                      "statuses": sorted({x[0] for x in latest.values() if x[0]})}
    return out


def versions(s, principal, key: str) -> dict:
    """One rule's (or mapping's) every version, newest first: what changed,
    the earlier settings kept on file, who and why."""
    from fastapi import HTTPException
    import audit_feed as af
    import audit_history as ah
    v, rows = _rows(s, principal, af.Filters())
    mine = [x for x in rows if x[3] == key]
    if not mine:
        raise HTTPException(404, "That rule is not one you can see.")
    people = ah.People(s, v)
    last = mine[-1][2]
    return {"key": key, "name": last.get("name"), "kind": last.get("kind"), "where": last.get("where"),
            "status": last.get("status"), "version": mine[-1][4],
            "versions": [{"event_id": e.id, "at": ch._iso(e.created_at), "version": version, "previous": prev,
                          "event": ch.EVENT_WORDS.get(det.get("event"), det.get("event")),
                          "changes": det.get("changes") or [], "more": int(det.get("more") or 0),
                          "before": det.get("before"), "cause": det.get("cause"), "reason": det.get("reason"),
                          "by": people.name(e.actor_user_id, "Kavachio · Automatic")}
                         for e, det, rule, k, version, prev in reversed(mine)]}


CSV_COLUMNS = (("at", "Date and time (UTC)"), ("name", "Rule"), ("kind", "Kind"), ("where", "Programme / contract"),
               ("version", "Version"), ("event", "What happened"), ("previous", "Previous version"),
               ("by", "Changed by"), ("reason", "Reason"))
