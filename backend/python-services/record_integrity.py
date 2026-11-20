"""
record_integrity.py
───────────────────
Task 52.1 — prove audit records were never changed.

  SEAL     Every audit record gets a fingerprint: SHA-256 of the record's own
           values (canonical(): keys sorted, empty values left out so a column
           added later does not change old fingerprints). Each seal is also
           chained to the one before it (seal_chain = SHA-256 of the previous
           chain + this record's fingerprint), so rewriting a fingerprint, or
           taking a seal out, breaks every link after it. Sealing runs in the
           daily sweep and before every check; a record is sealed once it is
           SETTLE_MINUTES old, so one still being written is never sealed.
  GUARD    Migration 50's trigger refuses UPDATE / DELETE / TRUNCATE on these
           tables, except for retention.py's own deletes.
  CHECK    check() works every fingerprint out again from the record as it is
           now and compares: Unchanged, Changed, Missing (sealed, now gone, and
           not deleted by retention), Deleted by retention. It also walks the
           whole chain. Each check is written to the Audit Trail
           ("integrity_checked"), which is where "last checked" comes from.

Kept: migration 50's audit_seal, on its own registry (IntegrityBase), so
init_db never builds it — a person applies the migration.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import BigInteger, Column, DateTime, Integer, String, text
from sqlalchemy.orm import declarative_base

IntegrityBase = declarative_base()

GENESIS = "0" * 64
SETTLE_MINUTES = 5
SEAL_BATCH = 20_000          # per table per run; the next run carries on
SHOWN_PROBLEMS = 200
CHECK_ACTION = "integrity_checked"
NOT_SET_UP = ("Integrity and retention are not set up on this database yet. "
              "Ask for migration 50 to be applied.")

# What is sealed: the table, its id column, its time column, its carrier
# column, and the words a person reads.
KINDS = {
    "activity_events":        {"id": "id", "at": "created_at", "tenant": "tenant_id",
                               "label": "Activity log", "info": "Everything people did, viewed and downloaded."},
    "auth_audit":             {"id": "id", "at": "created_at", "tenant": "tenant_id",
                               "label": "Sign-ins", "info": "Signing in and out, failed sign-ins, password resets."},
    "exception_decision_log": {"id": "id", "at": "decided_at", "tenant": "tenant_id",
                               "label": "Exception decisions",
                               "info": "Every decision on a data problem: accepted, corrected, dismissed."},
    "audit_finding_event":    {"id": "afe_id", "at": "afe_at", "tenant": "afe_tenant_id",
                               "label": "Audit finding history",
                               "info": "Every step on an audit finding, with its evidence."},
}
RESULT_WORDS = {"ok": "Unchanged", "changed": "Changed", "missing": "Missing", "gone": "Deleted by retention"}


class Seal(IntegrityBase):
    __tablename__ = "audit_seal"
    id = Column("seal_id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    table = Column("seal_table", String, nullable=False)
    record_id = Column("seal_record_id", BigInteger, nullable=False)
    tenant_id = Column("seal_tenant_id", Integer, nullable=True)
    record_at = Column("seal_record_at", DateTime, nullable=True)
    hash = Column("seal_hash", String, nullable=False)
    chain = Column("seal_chain", String, nullable=False)
    at = Column("seal_at", DateTime, default=dt.datetime.utcnow)
    gone_at = Column("seal_gone_at", DateTime, nullable=True)
    gone_run_id = Column("seal_gone_run_id", Integer, nullable=True)


TABLES = ("audit_seal", "retention_rule", "legal_hold", "retention_deletion")


def missing_tables(s) -> bool:
    from sqlalchemy import inspect
    insp = inspect(s.connection())
    return not all(insp.has_table(t) for t in TABLES)


def require_tables(s) -> None:
    if missing_tables(s):
        raise HTTPException(409, NOT_SET_UP)


# ── the fingerprint ────────────────────────────────────────────────────────
def _plain(v):
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "sha256:" + hashlib.sha256(bytes(v)).hexdigest()
    return str(v)


def canonical(row: dict) -> str:
    """The record's values as one fixed string. A value that is empty (None)
    is left out, so a column added to the table later leaves old records'
    fingerprints as they were."""
    vals = {}
    for k, v in row.items():
        if v is None:
            continue
        if isinstance(v, str) and v[:1] in ("{", "[") and k in ("details",):
            try:
                v = json.loads(v)       # sqlite hands JSON back as text, Postgres as a dict
            except ValueError:
                pass
        vals[k] = v
    return json.dumps(vals, sort_keys=True, default=_plain, separators=(",", ":"), ensure_ascii=False)


def fingerprint(row: dict) -> str:
    return hashlib.sha256(canonical(row).encode("utf-8")).hexdigest()


def link(prev: str, table: str, record_id, h: str) -> str:
    return hashlib.sha256(f"{prev}|{table}|{record_id}|{h}".encode("utf-8")).hexdigest()


# ── sealing ────────────────────────────────────────────────────────────────
def _last_chain(s) -> str:
    last = s.query(Seal.chain).order_by(Seal.id.desc()).first()
    return last[0] if last else GENESIS


def _lock(s) -> bool:
    """One sealer at a time, across app instances (Postgres); elsewhere, go on."""
    try:
        if s.get_bind().dialect.name != "postgresql":
            return True
        return bool(s.execute(text("SELECT pg_try_advisory_xact_lock(52010001)")).scalar())
    except Exception:  # noqa: BLE001
        return True


def seal_new(s, now: Optional[dt.datetime] = None) -> int:
    """Seal every settled record not sealed yet, in id order. Returns how many."""
    if missing_tables(s) or not _lock(s):
        return 0
    now = now or dt.datetime.utcnow()
    settled = now - dt.timedelta(minutes=SETTLE_MINUTES)
    prev = _last_chain(s)
    done = 0
    from sqlalchemy import inspect
    insp = inspect(s.connection())
    for table, k in KINDS.items():
        if not insp.has_table(table):
            continue
        rows = s.execute(text(
            f"SELECT t.* FROM {table} t WHERE t.{k['at']} < :settled AND NOT EXISTS ("
            f"SELECT 1 FROM audit_seal x WHERE x.seal_table = :tb AND x.seal_record_id = t.{k['id']}) "
            f"ORDER BY t.{k['id']} LIMIT :n"), {"settled": settled, "tb": table, "n": SEAL_BATCH}).mappings().all()
        for r in rows:
            row = dict(r)
            h = fingerprint(row)
            prev = link(prev, table, row[k["id"]], h)
            s.add(Seal(table=table, record_id=row[k["id"]], tenant_id=row.get(k["tenant"]),
                       record_at=as_time(row.get(k["at"])), hash=h, chain=prev, at=now))
            done += 1
        s.flush()
    s.commit()
    return done


# ── checking ───────────────────────────────────────────────────────────────
def _chain_ok(s) -> tuple[bool, Optional[int]]:
    """Walk every seal in order; the first one whose link does not follow."""
    prev = GENESIS
    for sid, table, rid, h, chain in (s.query(Seal.id, Seal.table, Seal.record_id, Seal.hash, Seal.chain)
                                      .order_by(Seal.id).yield_per(5000)):
        prev = link(prev, table, rid, h)
        if prev != chain:
            return False, sid
    return True, None


def check(s, tenant_id: Optional[int], by: Optional[str] = None, by_user_id=None, now=None) -> dict:
    """Every sealed record of this carrier (None: every carrier) worked out
    again and compared. Seals what is new first, so the newest settled
    records are included."""
    require_tables(s)
    now = now or dt.datetime.utcnow()
    seal_new(s, now)
    kinds, problems = [], []
    from sqlalchemy import inspect
    insp = inspect(s.connection())
    for table, k in KINDS.items():
        if not insp.has_table(table):
            continue
        q = s.query(Seal).filter(Seal.table == table)
        if tenant_id is not None:
            q = q.filter(Seal.tenant_id == tenant_id)
        seals = {x.record_id: x for x in q}
        counts = {"ok": 0, "changed": 0, "missing": 0, "gone": 0}
        live = [i for i, x in seals.items() if x.gone_at is None]
        rows = {}
        for i in range(0, len(live), 5000):
            part = live[i:i + 5000]
            for r in s.execute(text(f"SELECT * FROM {table} WHERE {k['id']} IN ({','.join(str(int(x)) for x in part)})")
                               ).mappings():
                rows[r[k["id"]]] = dict(r)
        for rid, x in seals.items():
            if x.gone_at is not None:
                counts["gone"] += 1
                continue
            row = rows.get(rid)
            state = "missing" if row is None else ("ok" if fingerprint(row) == x.hash else "changed")
            counts[state] += 1
            if state != "ok" and len(problems) < SHOWN_PROBLEMS:
                problems.append({"type": k["label"], "record": rid, "created": _iso(x.record_at),
                                 "sealed": _iso(x.at), "result": RESULT_WORDS[state]})
        unsealed = s.execute(text(f"SELECT COUNT(*) FROM {table} t WHERE "
                                  + (f"t.{k['tenant']} = :tid AND " if tenant_id is not None else "")
                                  + f"NOT EXISTS (SELECT 1 FROM audit_seal x WHERE x.seal_table = :tb "
                                    f"AND x.seal_record_id = t.{k['id']})"),
                             {"tid": tenant_id, "tb": table}).scalar() or 0
        kinds.append({"table": table, "type": k["label"], "info": k["info"], "checked": len(seals) - counts["gone"],
                      "unchanged": counts["ok"], "changed": counts["changed"], "missing": counts["missing"],
                      "gone": counts["gone"], "not_sealed_yet": int(unsealed),
                      "result": "Problem found" if counts["changed"] or counts["missing"] else "All unchanged"})
    chain_ok, broken_at = _chain_ok(s)
    bad = sum(x["changed"] + x["missing"] for x in kinds)
    result = {"at": _iso(now), "by": by, "chain_ok": chain_ok, "broken_at": broken_at, "kinds": kinds,
              "problems": problems, "checked": sum(x["checked"] for x in kinds), "problems_total": bad,
              "result": "All unchanged" if not bad and chain_ok else "Problem found"}
    from db import ActivityEvent
    s.add(ActivityEvent(tenant_id=tenant_id, actor=by, actor_user_id=by_user_id, action=CHECK_ACTION,
                        target=f"tenant:{tenant_id}" if tenant_id else "all",
                        details={"checked": result["checked"], "problems": bad, "chain": "whole" if chain_ok else "broken",
                                 "result": result["result"]}))
    s.commit()
    return result


def summary(s, tenant_id: Optional[int]) -> dict:
    """The tab's tiles and table without working everything out again: seals
    so far per kind, and the last check (from the Audit Trail)."""
    if missing_tables(s):
        return {"ready": False, "message": NOT_SET_UP}
    from sqlalchemy import func
    from db import ActivityEvent
    q = s.query(Seal.table, func.count(), func.count(Seal.gone_at), func.max(Seal.at))
    if tenant_id is not None:
        q = q.filter(Seal.tenant_id == tenant_id)
    got = {t: (n, g, at) for t, n, g, at in q.group_by(Seal.table)}
    lq = s.query(ActivityEvent).filter(ActivityEvent.action == CHECK_ACTION)
    lq = lq.filter(ActivityEvent.tenant_id == tenant_id) if tenant_id is not None else lq
    last = lq.order_by(ActivityEvent.created_at.desc()).first()
    d = (last.details if last and isinstance(last.details, dict) else {}) or {}
    kinds = [{"table": t, "type": k["label"], "info": k["info"], "sealed": int(got.get(t, (0, 0, None))[0]),
              "gone": int(got.get(t, (0, 0, None))[1]), "last_sealed": _iso(got.get(t, (0, 0, None))[2])}
             for t, k in KINDS.items()]
    return {"ready": True, "kinds": kinds, "sealed": sum(x["sealed"] - x["gone"] for x in kinds),
            "last_check": {"at": _iso(last.created_at), "by": last.actor, "result": d.get("result"),
                           "checked": d.get("checked"), "problems": d.get("problems")} if last else None,
            "settle_minutes": SETTLE_MINUTES}


def as_time(v) -> Optional[dt.datetime]:
    """A time from a raw query: Postgres gives a datetime, sqlite text."""
    if v is None or isinstance(v, dt.datetime):
        return v
    try:
        return dt.datetime.fromisoformat(str(v))
    except ValueError:
        return None


def _iso(x) -> Optional[str]:
    return x.isoformat() + "Z" if isinstance(x, dt.datetime) else None
