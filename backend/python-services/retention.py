"""
retention.py
────────────
Task 52.2 — delete records once their retention period has passed, unless a
legal hold keeps them, and keep a log of what was deleted.

  PERIODS   retention_rule: months per kind of record (RECORD_TYPES), for one
            country or everywhere (''). A carrier's country is its address's
            country (tenant.address.country); a rule for that country wins over
            the everywhere rule. No rule, or months left empty, keeps everything
            — so nothing is ever deleted until legal gives the periods and
            Kavachio enters them.
  HOLDS     legal_hold: on a whole carrier (nothing of it is deleted) or on one
            broker (its decisions, its audits, and what its own people did and
            signed in). A hold stays until someone lifts it, with a note.
  THE RUN   run_due(), in the daily sweep after sealing: per carrier and kind,
            everything older than the cut-off and not held is deleted, at most
            MAX_DELETE a run, inside one transaction that sets
            kavachio.retention_delete (the only way past migration 50's guard).
            Each deleted record's seal is marked gone, not removed, so the
            integrity chain stays whole. Every run that had something to delete
            (or held something back) leaves a retention_deletion row, which the
            guard keeps for good.
  NOT DELETED  files and bordereau data — this covers the audit records only.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import Column, DateTime, Integer, String, Text, func, text

import record_integrity as ri

log = logging.getLogger("retention")

MAX_DELETE = 50_000
DAYS_PER_MONTH = 30.44
EVERYWHERE = ""

# Each kind: its table, id, time, carrier and broker columns, and plain words.
RECORD_TYPES = {
    "activity":  {"label": "Activity log", "table": "activity_events", "id": "id", "at": "created_at",
                  "tenant": "tenant_id", "broker": "actor_broker_party_id",
                  "info": "Everything people did, viewed and downloaded, and every change kept in Change and Rule History."},
    "sign_in":   {"label": "Sign-ins", "table": "auth_audit", "id": "id", "at": "created_at", "tenant": "tenant_id",
                  "broker": None, "info": "Signing in and out, failed sign-ins and password resets."},
    "decisions": {"label": "Exception decisions", "table": "exception_decision_log", "id": "id", "at": "decided_at",
                  "tenant": "tenant_id", "broker": "broker_party_id",
                  "info": "Every decision made on a data problem in a file."},
    "audits":    {"label": "Closed audits", "table": "audit_engagement", "id": "au_id", "at": "au_closed_at",
                  "tenant": "au_tenant_id", "broker": "au_broker_party_id",
                  "info": "Audits closed that long ago, with their findings, replies and evidence."},
}


class Rule(ri.IntegrityBase):
    __tablename__ = "retention_rule"
    id = Column("rr_id", Integer, primary_key=True)
    record_type = Column("rr_record_type", String, nullable=False)
    country = Column("rr_country", String, nullable=False, default=EVERYWHERE)
    months = Column("rr_months", Integer, nullable=True)
    note = Column("rr_note", Text, nullable=True)
    updated_by_user_id = Column("rr_updated_by_user_id", Integer, nullable=True)
    updated_at = Column("rr_updated_at", DateTime, default=dt.datetime.utcnow)


class Hold(ri.IntegrityBase):
    __tablename__ = "legal_hold"
    id = Column("lh_id", Integer, primary_key=True)
    tenant_id = Column("lh_tenant_id", Integer, nullable=False)
    broker_party_id = Column("lh_broker_party_id", Integer, nullable=True)
    reason = Column("lh_reason", Text, nullable=False)
    placed_by_user_id = Column("lh_placed_by_user_id", Integer, nullable=True)
    placed_at = Column("lh_placed_at", DateTime, default=dt.datetime.utcnow)
    released_at = Column("lh_released_at", DateTime, nullable=True)
    released_by_user_id = Column("lh_released_by_user_id", Integer, nullable=True)
    release_note = Column("lh_release_note", Text, nullable=True)


class Deletion(ri.IntegrityBase):
    __tablename__ = "retention_deletion"
    id = Column("rd_id", Integer, primary_key=True)
    run_at = Column("rd_run_at", DateTime, default=dt.datetime.utcnow)
    tenant_id = Column("rd_tenant_id", Integer, nullable=True)
    record_type = Column("rd_record_type", String, nullable=False)
    country = Column("rd_country", String, nullable=True)
    months = Column("rd_months", Integer, nullable=False)
    cutoff = Column("rd_cutoff", DateTime, nullable=False)
    count = Column("rd_count", Integer, nullable=False, default=0)
    held = Column("rd_held", Integer, nullable=False, default=0)
    oldest = Column("rd_oldest", DateTime, nullable=True)
    newest = Column("rd_newest", DateTime, nullable=True)


# ── periods ────────────────────────────────────────────────────────────────
def country_of(s, tenant_id: Optional[int]) -> str:
    from db import Tenant
    t = s.get(Tenant, tenant_id) if tenant_id else None
    addr = t.address if t is not None and isinstance(t.address, dict) else {}
    return str(addr.get("country") or "").strip()


def months_for(rules: list[Rule], record_type: str, country: str) -> tuple[Optional[int], str]:
    """The period that applies, and where it comes from ('' = everywhere)."""
    mine = {r.country.strip().lower(): r for r in rules if r.record_type == record_type}
    r = (mine.get(country.lower()) if country else None) or mine.get(EVERYWHERE)
    return (r.months, r.country) if r is not None and r.months else (None, "")


def cutoff_for(months: int, now: dt.datetime) -> dt.datetime:
    return now - dt.timedelta(days=round(months * DAYS_PER_MONTH))


def save_rule(s, user_id: int, record_type: str, country: Optional[str], months: Optional[int],
              note: Optional[str]) -> Rule:
    errors = {}
    if record_type not in RECORD_TYPES:
        errors["record_type"] = "Pick a kind of record."
    if months is not None and not (1 <= int(months) <= 1200):
        errors["months"] = "Enter between 1 and 1,200 months, or leave it empty to keep everything."
    if errors:
        raise HTTPException(422, {"errors": errors})
    country = (country or "").strip()[:80]
    r = s.query(Rule).filter(Rule.record_type == record_type, func.lower(Rule.country) == country.lower()).first()
    if r is None:
        r = Rule(record_type=record_type, country=country)
        s.add(r)
    r.months, r.note = (int(months) if months else None), ((note or "").strip() or None)
    r.updated_by_user_id, r.updated_at = user_id, dt.datetime.utcnow()
    s.flush()
    return r


def remove_rule(s, rule_id: int) -> Rule:
    r = s.get(Rule, rule_id)
    if r is None:
        raise HTTPException(404, "That retention period is not there any more.")
    s.delete(r)
    return r


# ── holds ──────────────────────────────────────────────────────────────────
def place_hold(s, tenant_id: int, user_id: int, reason: Optional[str], broker_party_id: Optional[int]) -> Hold:
    if not (reason or "").strip():
        raise HTTPException(422, {"errors": {"reason": "Say why the records must be kept."}})
    if broker_party_id:
        import audit_feed as af
        if broker_party_id not in af.brokers_of_carrier(s, tenant_id):
            raise HTTPException(422, {"errors": {"broker_party_id": "Pick one of this carrier's brokers."}})
    h = Hold(tenant_id=tenant_id, broker_party_id=broker_party_id or None, reason=reason.strip()[:2000],
             placed_by_user_id=user_id, placed_at=dt.datetime.utcnow())
    s.add(h)
    s.flush()
    return h


def lift_hold(s, tenant_id: Optional[int], hold_id: int, user_id: int, note: Optional[str]) -> Hold:
    h = s.get(Hold, hold_id)
    if h is None or (tenant_id is not None and h.tenant_id != tenant_id):
        raise HTTPException(404, "That hold is not one of yours.")
    if h.released_at is not None:
        raise HTTPException(409, "That hold was already lifted.")
    if not (note or "").strip():
        raise HTTPException(422, {"errors": {"note": "Say why the hold can be lifted."}})
    h.released_at, h.released_by_user_id, h.release_note = dt.datetime.utcnow(), user_id, note.strip()[:2000]
    return h


def _active_holds(s, tenant_id: int) -> tuple[bool, set[int]]:
    """(the whole carrier is held, the brokers held)."""
    rows = s.query(Hold.broker_party_id).filter(Hold.tenant_id == tenant_id, Hold.released_at.is_(None)).all()
    return any(b is None for (b,) in rows), {b for (b,) in rows if b}


# ── which records ──────────────────────────────────────────────────────────
def _where(kind: str, tenant_id: Optional[int], held_brokers: set[int]) -> tuple[str, dict]:
    k = RECORD_TYPES[kind]
    sql = f"t.{k['at']} < :cutoff"
    params: dict = {}
    if tenant_id is None:
        sql += f" AND t.{k['tenant']} IS NULL"
    else:
        sql += f" AND t.{k['tenant']} = :tid"
        params["tid"] = tenant_id
    if held_brokers:
        ids = ",".join(str(int(b)) for b in sorted(held_brokers))
        if k["broker"]:
            sql += f" AND (t.{k['broker']} IS NULL OR t.{k['broker']} NOT IN ({ids}))"
        if kind == "sign_in":
            sql += (f" AND NOT EXISTS (SELECT 1 FROM app_user u WHERE u.user_id = t.user_id "
                    f"AND u.broker_party_id IN ({ids}))")
    if kind == "audits":
        sql += " AND t.au_status = 'closed'"
    return sql, params


def _count(s, kind: str, tenant_id, held: set[int], cutoff) -> tuple[int, Optional[dt.datetime]]:
    k = RECORD_TYPES[kind]
    where, params = _where(kind, tenant_id, held)
    n, oldest = s.execute(text(f"SELECT COUNT(*), MIN(t.{k['at']}) FROM {k['table']} t WHERE {where}"),
                          {**params, "cutoff": cutoff}).one()
    return int(n or 0), ri.as_time(oldest)


def _tenants(s) -> list[Optional[int]]:
    from db import Tenant
    return [None] + [t for (t,) in s.query(Tenant.id).order_by(Tenant.id)]


def _has_table(s, table: str) -> bool:
    from sqlalchemy import inspect
    return inspect(s.connection()).has_table(table)


# ── the run ────────────────────────────────────────────────────────────────
def _delete(s, kind: str, tenant_id, held: set[int], cutoff) -> tuple[int, Optional[dt.datetime], Optional[dt.datetime]]:
    """Delete one batch of one kind, mark its seals gone. In the caller's transaction."""
    k = RECORD_TYPES[kind]
    where, params = _where(kind, tenant_id, held)
    if s.get_bind().dialect.name == "postgresql":
        s.execute(text("SET LOCAL kavachio.retention_delete = 'on'"))
    ids = s.execute(text(f"SELECT t.{k['id']}, t.{k['at']} FROM {k['table']} t WHERE {where} "
                         f"ORDER BY t.{k['at']} LIMIT :n"), {**params, "cutoff": cutoff, "n": MAX_DELETE}).all()
    if not ids:
        return 0, None, None
    keys = [i for i, _ in ids]
    whens = [ri.as_time(w) for _, w in ids if w is not None]
    sealed = {"audits": None}.get(kind, k["table"])
    for i in range(0, len(keys), 5000):
        part = ",".join(str(int(x)) for x in keys[i:i + 5000])
        if kind == "audits":
            # Their findings' history goes with them (ON DELETE CASCADE); mark those seals gone first.
            ev = [e for (e,) in s.execute(text(
                f"SELECT e.afe_id FROM audit_finding_event e JOIN audit_finding f ON f.af_id = e.afe_finding_id "
                f"WHERE f.af_audit_id IN ({part})"))]
            _gone(s, "audit_finding_event", ev)
        else:
            _gone(s, sealed, [int(x) for x in keys[i:i + 5000]])
        s.execute(text(f"DELETE FROM {k['table']} WHERE {k['id']} IN ({part})"))
    return len(keys), min(whens) if whens else None, max(whens) if whens else None


def _gone(s, table: str, ids: list[int]) -> None:
    if not ids:
        return
    now = dt.datetime.utcnow()
    for i in range(0, len(ids), 5000):
        s.query(ri.Seal).filter(ri.Seal.table == table, ri.Seal.record_id.in_(ids[i:i + 5000])).update(
            {ri.Seal.gone_at: now}, synchronize_session=False)


def run_due(s, now: Optional[dt.datetime] = None) -> int:
    """Every carrier, every kind with a period: delete what is past it and not
    held. Returns how many records were deleted."""
    if ri.missing_tables(s):
        return 0
    now = now or dt.datetime.utcnow()
    rules = s.query(Rule).all()
    if not any(r.months for r in rules):
        return 0
    total = 0
    for tid in _tenants(s):
        country = country_of(s, tid)
        whole, brokers = _active_holds(s, tid) if tid is not None else (False, set())
        for kind, k in RECORD_TYPES.items():
            months, used = months_for(rules, kind, country)
            if not months or not _has_table(s, k["table"]):
                continue
            cutoff = cutoff_for(months, now)
            try:
                if whole:
                    n, oldest = _count(s, kind, tid, set(), cutoff)
                    if n:
                        s.add(Deletion(run_at=now, tenant_id=tid, record_type=kind, country=used or None,
                                       months=months, cutoff=cutoff, count=0, held=n, oldest=oldest))
                        s.commit()
                    continue
                held = (_count(s, kind, tid, set(), cutoff)[0] - _count(s, kind, tid, brokers, cutoff)[0]
                        if brokers else 0)
                n, oldest, newest = _delete(s, kind, tid, brokers, cutoff)
                if n or held:
                    s.add(Deletion(run_at=now, tenant_id=tid, record_type=kind, country=used or None,
                                   months=months, cutoff=cutoff, count=n, held=held, oldest=oldest, newest=newest))
                s.commit()
                total += n
            except Exception:  # noqa: BLE001 — one carrier's failure must not stop the rest
                s.rollback()
                log.exception("retention: %s for carrier %s failed", kind, tid)
    return total


# ── what the screen shows ──────────────────────────────────────────────────
def overview(s, tenant_id: Optional[int], now: Optional[dt.datetime] = None) -> dict:
    """Per kind: the period that applies, the oldest record, what the next run
    would delete. Then the holds and the deletion log."""
    if ri.missing_tables(s):
        return {"ready": False, "message": ri.NOT_SET_UP}
    now = now or dt.datetime.utcnow()
    rules = s.query(Rule).order_by(Rule.record_type, Rule.country).all()
    country = country_of(s, tenant_id) if tenant_id else ""
    whole, brokers = _active_holds(s, tenant_id) if tenant_id else (False, set())
    kinds = []
    for kind, k in RECORD_TYPES.items():
        months, used = months_for(rules, kind, country)
        row = {"key": kind, "type": k["label"], "info": k["info"], "months": months,
               "applies": (used or "Everywhere") if months else None, "oldest": None,
               "next_count": 0, "held": 0, "cutoff": None}
        if _has_table(s, k["table"]) and tenant_id is not None:
            at = k["at"]
            extra = " AND au_status = 'closed'" if kind == "audits" else ""
            row["oldest"] = ri._iso(ri.as_time(s.execute(text(f"SELECT MIN({at}) FROM {k['table']} WHERE {k['tenant']} = :t{extra}"),
                                              {"t": tenant_id}).scalar()))
            if months:
                cutoff = cutoff_for(months, now)
                row["cutoff"] = ri._iso(cutoff)
                n_all = _count(s, kind, tenant_id, set(), cutoff)[0]
                n = 0 if whole else _count(s, kind, tenant_id, brokers, cutoff)[0]
                row.update(next_count=n, held=n_all - n)
        kinds.append(row)
    return {"ready": True, "country": country or None, "kinds": kinds, "whole_held": whole,
            "rules": [{"id": r.id, "key": r.record_type, "type": RECORD_TYPES.get(r.record_type, {}).get("label"),
                       "country": r.country or "Everywhere", "months": r.months, "note": r.note,
                       "updated_at": ri._iso(r.updated_at)} for r in rules],
            "types": [{"id": key, "name": v["label"]} for key, v in RECORD_TYPES.items()]}


def holds(s, tenant_id: Optional[int]) -> list[dict]:
    from db import AppUser, Party, Tenant
    q = s.query(Hold)
    if tenant_id is not None:
        q = q.filter(Hold.tenant_id == tenant_id)
    rows = q.order_by(Hold.released_at.isnot(None), Hold.placed_at.desc()).all()
    users = {u.id: u.full_name or u.email for u in s.query(AppUser).filter(
        AppUser.id.in_({x for h in rows for x in (h.placed_by_user_id, h.released_by_user_id) if x} or {0}))}
    parties = dict(s.query(Party.id, Party.legal_name).filter(Party.id.in_({h.broker_party_id for h in rows} or {0})))
    carriers = {t.id: t.legal_name or t.tenant_name for t in
                s.query(Tenant).filter(Tenant.id.in_({h.tenant_id for h in rows} or {0}))}
    return [{"id": h.id, "carrier": carriers.get(h.tenant_id), "covers": parties.get(h.broker_party_id)
             if h.broker_party_id else "Everything of this carrier", "broker_id": h.broker_party_id,
             "reason": h.reason, "placed_at": ri._iso(h.placed_at), "placed_by": users.get(h.placed_by_user_id),
             "on": h.released_at is None, "lifted_at": ri._iso(h.released_at),
             "lifted_by": users.get(h.released_by_user_id), "lift_note": h.release_note} for h in rows]


def deletions(s, tenant_id: Optional[int], page: int = 1, page_size: int = 50) -> dict:
    from db import Tenant
    q = s.query(Deletion)
    if tenant_id is not None:
        q = q.filter(Deletion.tenant_id == tenant_id)
    total = q.count()
    rows = q.order_by(Deletion.run_at.desc(), Deletion.id.desc()).offset((max(page, 1) - 1) * page_size).limit(page_size).all()
    carriers = {t.id: t.legal_name or t.tenant_name for t in
                s.query(Tenant).filter(Tenant.id.in_({d.tenant_id for d in rows if d.tenant_id} or {0}))}
    return {"items": [{"id": d.id, "at": ri._iso(d.run_at), "carrier": carriers.get(d.tenant_id) or "Kavachio",
                       "type": RECORD_TYPES.get(d.record_type, {}).get("label", d.record_type),
                       "months": d.months, "country": d.country or "Everywhere", "cutoff": ri._iso(d.cutoff),
                       "deleted": d.count, "held": d.held, "oldest": ri._iso(d.oldest), "newest": ri._iso(d.newest)}
                      for d in rows], "total": total, "page": page, "page_size": page_size}
