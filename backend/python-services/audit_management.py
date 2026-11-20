"""
audit_management.py
───────────────────
Audit Management (tasks 53.1, 53.2): audits a carrier records against its
brokers and their contracts, and each finding followed until it is closed.

WHO
  The carrier creates and edits audits and findings, and is the only one who
  closes (or reopens) a finding or an audit. A broker sees only the findings
  about itself — never another broker's — and may reply and add evidence.
  Kavachio reads, and changes nothing.

STATUS
  An audit is Planned until its start date or its first finding, then In
  progress, then Closed — closed only once every finding is closed. A finding
  is Open, In progress (the carrier says so, or the broker replied), Closed
  (with how it was fixed). Every step is an audit_finding_event, so a
  finding's history is never rewritten.

STORAGE (migration 49), on its own model registry (AuditBase), so init_db's
create_all never builds these tables: a person applies the migration.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import (Column, Date, DateTime, ForeignKey, Integer, LargeBinary, String, Text)
from sqlalchemy.orm import declarative_base

AuditBase = declarative_base()

TYPES = {"annual": "Annual audit", "contract_compliance": "Contract compliance",
         "claims": "Claims audit", "data_quality": "Data quality", "other": "Other"}
SEVERITIES = {"low": "Low", "medium": "Medium", "high": "High", "critical": "Critical"}
FINDING_STATUS = {"open": "Open", "in_progress": "In progress", "closed": "Closed"}
AUDIT_STATUS = {"planned": "Planned", "in_progress": "In progress", "closed": "Closed"}
EVENT_WORDS = {"created": "Finding raised", "reply": "Reply", "evidence": "Evidence added",
               "status": "Status changed", "closed": "Finding closed", "reopened": "Finding reopened",
               "edited": "Finding changed"}
MAX_EVIDENCE_BYTES = 20 * 1024 * 1024

NOT_SET_UP = ("Audits are not set up on this database yet. "
              "Ask for migration 49 to be applied.")


class Audit(AuditBase):
    __tablename__ = "audit_engagement"
    id = Column("au_id", Integer, primary_key=True)
    tenant_id = Column("au_tenant_id", Integer, nullable=False)
    name = Column("au_name", String, nullable=False)
    type = Column("au_type", String, nullable=False, default="other")
    broker_party_id = Column("au_broker_party_id", Integer, nullable=False)
    contract_id = Column("au_contract_id", Integer, nullable=True)
    scope = Column("au_scope", Text, nullable=True)
    assigned_to = Column("au_assigned_to", String, nullable=True)
    start_date = Column("au_start_date", Date, nullable=True)
    due_date = Column("au_due_date", Date, nullable=True)
    status = Column("au_status", String, nullable=False, default="planned")
    closing_note = Column("au_closing_note", Text, nullable=True)
    closed_at = Column("au_closed_at", DateTime, nullable=True)
    closed_by_user_id = Column("au_closed_by_user_id", Integer, nullable=True)
    created_by_user_id = Column("au_created_by_user_id", Integer, nullable=True)
    created_at = Column("au_created_at", DateTime, default=dt.datetime.utcnow)
    updated_at = Column("au_updated_at", DateTime, default=dt.datetime.utcnow)


class Finding(AuditBase):
    __tablename__ = "audit_finding"
    id = Column("af_id", Integer, primary_key=True)
    audit_id = Column("af_audit_id", Integer, ForeignKey("audit_engagement.au_id", ondelete="CASCADE"), nullable=False)
    tenant_id = Column("af_tenant_id", Integer, nullable=False)
    broker_party_id = Column("af_broker_party_id", Integer, nullable=False)
    title = Column("af_title", Text, nullable=False)
    severity = Column("af_severity", String, nullable=False, default="medium")
    action = Column("af_action", Text, nullable=True)
    owner = Column("af_owner", String, nullable=True)
    due_date = Column("af_due_date", Date, nullable=True)
    status = Column("af_status", String, nullable=False, default="open")
    resolution = Column("af_resolution", Text, nullable=True)
    closed_at = Column("af_closed_at", DateTime, nullable=True)
    closed_by_user_id = Column("af_closed_by_user_id", Integer, nullable=True)
    created_by_user_id = Column("af_created_by_user_id", Integer, nullable=True)
    created_at = Column("af_created_at", DateTime, default=dt.datetime.utcnow)
    updated_at = Column("af_updated_at", DateTime, default=dt.datetime.utcnow)


class FindingEvent(AuditBase):
    __tablename__ = "audit_finding_event"
    id = Column("afe_id", Integer, primary_key=True)
    finding_id = Column("afe_finding_id", Integer, ForeignKey("audit_finding.af_id", ondelete="CASCADE"),
                        nullable=False)
    tenant_id = Column("afe_tenant_id", Integer, nullable=False)
    kind = Column("afe_kind", String, nullable=False)
    note = Column("afe_note", Text, nullable=True)
    from_status = Column("afe_from_status", String, nullable=True)
    to_status = Column("afe_to_status", String, nullable=True)
    file_name = Column("afe_file_name", String, nullable=True)
    file_ref = Column("afe_file_ref", String, nullable=True)
    file_bytes = Column("afe_file_bytes", LargeBinary, nullable=True)
    by_user_id = Column("afe_by_user_id", Integer, nullable=True)
    by_side = Column("afe_by_side", String, nullable=True)
    at = Column("afe_at", DateTime, default=dt.datetime.utcnow)


# ── the database ───────────────────────────────────────────────────────────
def missing_tables(s) -> bool:
    """True when migration 49 has not been applied to this database."""
    from sqlalchemy import inspect
    insp = inspect(s.connection())
    return not all(insp.has_table(t) for t in ("audit_engagement", "audit_finding", "audit_finding_event"))


def require_tables(s) -> None:
    if missing_tables(s):
        raise HTTPException(409, NOT_SET_UP)


# ── reading ────────────────────────────────────────────────────────────────
def _today() -> dt.date:
    return dt.datetime.utcnow().date()


def shown_status(a: Audit, has_findings: bool, today: Optional[dt.date] = None) -> str:
    """Planned until it starts or gets a finding; then In progress; Closed."""
    if a.status == "closed":
        return "closed"
    today = today or _today()
    if has_findings or (a.start_date and a.start_date <= today) or a.status == "in_progress":
        return "in_progress"
    return "planned"


def overdue(due: Optional[dt.date], status: str, today: Optional[dt.date] = None) -> bool:
    return bool(due and status != "closed" and due < (today or _today()))


def _iso(x) -> Optional[str]:
    if x is None:
        return None
    return x.isoformat() + "Z" if isinstance(x, dt.datetime) else x.isoformat()


def _names(s, party_ids=(), contract_ids=(), user_ids=()) -> dict:
    from db import AppUser, Contract, Party
    p = {i for i in party_ids if i}
    c = {i for i in contract_ids if i}
    u = {i for i in user_ids if i}
    return {"party": dict(s.query(Party.id, Party.legal_name).filter(Party.id.in_(p))) if p else {},
            "contract": {x.id: x.name or x.filename or f"Contract #{x.id}"
                         for x in s.query(Contract).filter(Contract.id.in_(c))} if c else {},
            "user": {x.id: x.full_name or x.email for x in s.query(AppUser).filter(AppUser.id.in_(u))} if u else {}}


def audit_ref(a: Audit) -> str:
    return f"AUD-{a.id:03d}"


def finding_view(f: Finding, events: list[FindingEvent], names: dict, viewer: str,
                 carrier_name: Optional[str] = None, today: Optional[dt.date] = None) -> dict:
    """One finding as a screen shows it. A broker sees the carrier's people as
    the carrier's name, never as a person."""
    def by(e: FindingEvent) -> str:
        if e.by_user_id is None:
            return "Kavachio · Automatic"
        if e.by_side == "broker":
            return names["user"].get(e.by_user_id) if viewer == "broker" else names["party"].get(f.broker_party_id) or "The broker"
        if viewer == "broker":
            return carrier_name or "The carrier"
        return names["user"].get(e.by_user_id) or "The carrier"
    return {
        "id": f.id, "audit_id": f.audit_id, "title": f.title,
        "severity": f.severity, "severity_label": SEVERITIES.get(f.severity, f.severity),
        "action": f.action, "owner": f.owner, "due_date": _iso(f.due_date),
        "status": f.status, "status_label": FINDING_STATUS.get(f.status, f.status),
        "overdue": overdue(f.due_date, f.status, today), "resolution": f.resolution,
        "closed_at": _iso(f.closed_at), "created_at": _iso(f.created_at),
        "broker": names["party"].get(f.broker_party_id),
        "history": [{"id": e.id, "kind": e.kind, "title": EVENT_WORDS.get(e.kind, e.kind), "note": e.note,
                     "from": FINDING_STATUS.get(e.from_status or "", None),
                     "to": FINDING_STATUS.get(e.to_status or "", None),
                     "file": e.file_name if (e.file_ref or e.file_bytes) else None,
                     "by": by(e), "side": e.by_side, "at": _iso(e.at)} for e in events],
    }


def _events(s, finding_ids) -> dict[int, list[FindingEvent]]:
    out: dict[int, list[FindingEvent]] = {}
    if not finding_ids:
        return out
    for e in (s.query(FindingEvent).filter(FindingEvent.finding_id.in_(list(finding_ids)))
              .order_by(FindingEvent.at.asc(), FindingEvent.id.asc())):
        out.setdefault(e.finding_id, []).append(e)
    return out


def audit_view(s, a: Audit, findings: list[Finding], names: dict, today=None, events=None) -> dict:
    st = shown_status(a, bool(findings), today)
    open_n = sum(1 for f in findings if f.status != "closed")
    out = {
        "id": a.id, "ref": audit_ref(a), "name": a.name, "type": a.type, "type_label": TYPES.get(a.type, a.type),
        "broker_id": a.broker_party_id, "broker": names["party"].get(a.broker_party_id),
        "contract_id": a.contract_id, "contract": names["contract"].get(a.contract_id),
        "scope": a.scope, "assigned_to": a.assigned_to,
        "start_date": _iso(a.start_date), "due_date": _iso(a.due_date),
        "status": st, "status_label": AUDIT_STATUS[st], "overdue": overdue(a.due_date, st, today),
        "findings_total": len(findings), "findings_open": open_n,
        "closing_note": a.closing_note, "closed_at": _iso(a.closed_at),
        "created_at": _iso(a.created_at),
    }
    if events is not None:
        out["findings"] = [finding_view(f, events.get(f.id, []), names, "carrier", today=today)
                           for f in sorted(findings, key=lambda f: (f.status == "closed", -_rank(f.severity), f.id))]
    return out


def _rank(sev: str) -> int:
    return list(SEVERITIES).index(sev) if sev in SEVERITIES else 0


def list_audits(s, tenant_id: Optional[int], *, broker=None, status=None, since=None, until=None,
                today: Optional[dt.date] = None) -> dict:
    today = today or _today()
    q = s.query(Audit)
    if tenant_id is not None:                     # None: Kavachio, every carrier
        q = q.filter(Audit.tenant_id == tenant_id)
    if broker:
        q = q.filter(Audit.broker_party_id == broker)
    if since:
        q = q.filter(Audit.created_at >= dt.datetime.combine(since, dt.time()))
    if until:
        q = q.filter(Audit.created_at < dt.datetime.combine(until, dt.time()) + dt.timedelta(days=1))
    audits = q.order_by(Audit.id.desc()).all()
    fs: dict[int, list[Finding]] = {}
    if audits:
        for f in s.query(Finding).filter(Finding.audit_id.in_([a.id for a in audits])):
            fs.setdefault(f.audit_id, []).append(f)
    names = _names(s, {a.broker_party_id for a in audits}, {a.contract_id for a in audits})
    rows = [audit_view(s, a, fs.get(a.id, []), names, today) for a in audits]
    if status:
        rows = [r for r in rows if r["status"] == status]
    every = [f for a in audits for f in fs.get(a.id, [])]
    return {"ready": True, "audits": rows,
            "counts": {"total": len(rows), "in_progress": sum(1 for r in rows if r["status"] == "in_progress"),
                       "open_findings": sum(1 for f in every if f.status != "closed"),
                       "closed": sum(1 for r in rows if r["status"] == "closed")},
            "types": [{"key": k, "label": v} for k, v in TYPES.items()],
            "severities": [{"key": k, "label": v} for k, v in SEVERITIES.items()]}


def get_audit(s, tenant_id: Optional[int], audit_id: int) -> Audit:
    a = s.get(Audit, audit_id)
    if a is None or (tenant_id is not None and a.tenant_id != tenant_id):
        raise HTTPException(404, "That audit is not one of yours.")
    return a


def one_audit(s, a: Audit) -> dict:
    findings = s.query(Finding).filter(Finding.audit_id == a.id).all()
    events = _events(s, [f.id for f in findings])
    names = _names(s, {a.broker_party_id}, {a.contract_id},
                   {e.by_user_id for es in events.values() for e in es})
    return audit_view(s, a, findings, names, events=events)


# ── writing (the carrier) ──────────────────────────────────────────────────
def _text(v, field: str, label: str, errors: dict, required=False, n=2000) -> Optional[str]:
    t = (v or "").strip() if isinstance(v, str) else ("" if v is None else str(v).strip())
    if required and not t:
        errors[field] = f"{label} is needed."
    return t[:n] or None


def _date(v, field: str, errors: dict) -> Optional[dt.date]:
    if v in (None, ""):
        return None
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        errors[field] = "Pick a real date."
        return None


def _bad(errors: dict):
    if errors:
        raise HTTPException(400, {"message": "Please fix the highlighted fields.", "errors": errors})


def clean_audit(s, tenant_id: int, body: dict) -> dict:
    """The audit form, checked: name, type, broker (one of this carrier's),
    contract (that broker's, on this carrier), dates in order."""
    from db import Contract
    import audit_feed as af
    errors: dict = {}
    out = {"name": _text(body.get("name"), "name", "Audit name", errors, True, 200),
           "type": body.get("type") if body.get("type") in TYPES else None,
           "scope": _text(body.get("scope"), "scope", "What it covers", errors),
           "assigned_to": _text(body.get("assigned_to"), "assigned_to", "Assigned to", errors, n=200),
           "start_date": _date(body.get("start_date"), "start_date", errors),
           "due_date": _date(body.get("due_date"), "due_date", errors)}
    if not out["type"]:
        errors["type"] = "Pick the type of audit."
    try:
        broker = int(body.get("broker_party_id") or 0)
    except (TypeError, ValueError):
        broker = 0
    if not broker or broker not in af.brokers_of_carrier(s, tenant_id):
        errors["broker_party_id"] = "Pick one of your brokers."
    out["broker_party_id"] = broker
    cid = body.get("contract_id")
    out["contract_id"] = None
    if cid not in (None, "", 0):
        c = s.get(Contract, int(cid))
        if c is None or c.tenant_id != tenant_id or c.broker_party_id != broker:
            errors["contract_id"] = "Pick one of this broker's contracts."
        else:
            out["contract_id"] = c.id
    if out["start_date"] and out["due_date"] and out["due_date"] < out["start_date"]:
        errors["due_date"] = "The due date must be on or after the start date."
    _bad(errors)
    return out


def create_audit(s, tenant_id: int, user_id: int, body: dict) -> Audit:
    a = Audit(tenant_id=tenant_id, created_by_user_id=user_id, status="planned", **clean_audit(s, tenant_id, body))
    s.add(a)
    s.flush()
    return a


def update_audit(s, a: Audit, body: dict) -> Audit:
    if a.status == "closed":
        raise HTTPException(409, "This audit is closed. Reopen a finding first if something changed.")
    had = a.broker_party_id
    for k, v in clean_audit(s, a.tenant_id, body).items():
        setattr(a, k, v)
    if a.broker_party_id != had and s.query(Finding).filter(Finding.audit_id == a.id).count():
        raise HTTPException(409, "This audit already has findings about its broker, so the broker cannot change.")
    a.updated_at = dt.datetime.utcnow()
    return a


def close_audit(s, a: Audit, user_id: int, note: Optional[str]) -> Audit:
    if a.status == "closed":
        return a
    still = s.query(Finding).filter(Finding.audit_id == a.id, Finding.status != "closed").count()
    if still:
        raise HTTPException(409, f"Close its {still} open {'finding' if still == 1 else 'findings'} first.")
    a.status, a.closing_note = "closed", (note or "").strip() or None
    a.closed_at, a.closed_by_user_id, a.updated_at = dt.datetime.utcnow(), user_id, dt.datetime.utcnow()
    return a


def clean_finding(body: dict) -> dict:
    errors: dict = {}
    out = {"title": _text(body.get("title"), "title", "What was found", errors, True),
           "severity": body.get("severity") if body.get("severity") in SEVERITIES else None,
           "action": _text(body.get("action"), "action", "What must be done", errors),
           "owner": _text(body.get("owner"), "owner", "Owner", errors, n=200),
           "due_date": _date(body.get("due_date"), "due_date", errors)}
    if not out["severity"]:
        errors["severity"] = "Pick how serious it is."
    _bad(errors)
    return out


def _event(s, f: Finding, kind: str, user_id: Optional[int], side: str, note=None,
           frm=None, to=None, file_name=None, data: Optional[bytes] = None) -> FindingEvent:
    ref = blob = None
    if data:
        import storage
        ref, blob = storage.store_or_keep("audit-evidence", f.tenant_id, file_name or "evidence", data)
    e = FindingEvent(finding_id=f.id, tenant_id=f.tenant_id, kind=kind, note=note, from_status=frm,
                     to_status=to, file_name=file_name if data else None, file_ref=ref, file_bytes=blob,
                     by_user_id=user_id, by_side=side, at=dt.datetime.utcnow())
    s.add(e)
    return e


def add_finding(s, a: Audit, user_id: int, body: dict) -> Finding:
    if a.status == "closed":
        raise HTTPException(409, "This audit is closed, so it cannot get new findings.")
    f = Finding(audit_id=a.id, tenant_id=a.tenant_id, broker_party_id=a.broker_party_id,
                created_by_user_id=user_id, status="open", **clean_finding(body))
    s.add(f)
    s.flush()
    _event(s, f, "created", user_id, "carrier", to="open")
    if a.status == "planned":
        a.status = "in_progress"
    return f


def get_finding(s, a: Audit, finding_id: int) -> Finding:
    f = s.get(Finding, finding_id)
    if f is None or f.audit_id != a.id:
        raise HTTPException(404, "That finding is not on this audit.")
    return f


def update_finding(s, f: Finding, user_id: int, body: dict) -> Finding:
    if f.status == "closed":
        raise HTTPException(409, "This finding is closed. Reopen it to change it.")
    new = clean_finding(body)
    changed = [k for k, v in new.items() if getattr(f, k) != v]
    for k, v in new.items():
        setattr(f, k, v)
    f.updated_at = dt.datetime.utcnow()
    if changed:
        _event(s, f, "edited", user_id, "carrier", note="Changed: " + ", ".join(
            {"title": "what was found", "severity": "how serious", "action": "what must be done",
             "owner": "owner", "due_date": "due date"}[k] for k in changed))
    return f


def set_status(s, a: Audit, f: Finding, user_id: int, status: str, note: Optional[str]) -> Finding:
    """The carrier moves a finding: In progress, Closed (saying how it was
    fixed), or Open again (reopening a closed one)."""
    note = (note or "").strip() or None
    if status not in FINDING_STATUS:
        raise HTTPException(400, "Pick Open, In progress or Closed.")
    if status == f.status:
        return f
    if status == "closed" and not note:
        raise HTTPException(400, {"message": "Say how it was fixed.", "errors": {"note": "How was it fixed?"}})
    if a.status == "closed":
        a.status = "in_progress"       # reopening a finding reopens its audit
        a.closed_at = a.closed_by_user_id = None
    frm = f.status
    kind = "closed" if status == "closed" else "reopened" if frm == "closed" else "status"
    f.status, f.updated_at = status, dt.datetime.utcnow()
    if status == "closed":
        f.resolution, f.closed_at, f.closed_by_user_id = note, dt.datetime.utcnow(), user_id
    elif frm == "closed":
        f.closed_at = f.closed_by_user_id = None
    _event(s, f, kind, user_id, "carrier", note=note, frm=frm, to=status)
    return f


def check_file(filename: str, data: bytes) -> None:
    """The same safety checks as every file Kavachio takes in, before it is kept."""
    import intake_safety as safety
    if not data:
        raise HTTPException(400, "That file is empty.")
    for reason in (safety.check_size(data, MAX_EVIDENCE_BYTES),
                   safety.check_safe_to_open(filename, data),
                   safety.scan_for_malware(filename, data)):
        if reason:
            raise HTTPException(400, f"That file cannot be added: {reason}")


def reply(s, f: Finding, user_id: int, side: str, note: Optional[str], file_name=None,
          data: Optional[bytes] = None) -> Finding:
    """A reply, with or without a file, from either side. A broker's first word
    on an open finding moves it to In progress."""
    note = (note or "").strip() or None
    if not note and not data:
        raise HTTPException(400, "Write a reply or add a file.")
    if f.status == "closed":
        raise HTTPException(409, "This finding is closed.")
    if data:
        check_file(file_name or "evidence", data)
    _event(s, f, "evidence" if data else "reply", user_id, side, note=note, file_name=file_name, data=data)
    if side == "broker" and f.status == "open":
        _event(s, f, "status", None, "broker", frm="open", to="in_progress",
               note="Moved on because the broker replied.")
        f.status = "in_progress"
    f.updated_at = dt.datetime.utcnow()
    return f


def evidence_bytes(s, f: Finding, event_id: int) -> tuple[str, bytes]:
    e = s.get(FindingEvent, event_id)
    if e is None or e.finding_id != f.id or not (e.file_ref or e.file_bytes):
        raise HTTPException(404, "That file is not on this finding.")
    import storage
    data = storage.resolve_bytes(e.file_ref, e.file_bytes)
    if data is None:
        raise HTTPException(404, "That file is no longer stored.")
    return e.file_name or "evidence", data


# ── the broker ─────────────────────────────────────────────────────────────
def broker_findings(s, broker_party_id: int, today=None) -> dict:
    """Every finding about this broker, from every carrier — open ones first."""
    from db import Tenant
    fs = s.query(Finding).filter(Finding.broker_party_id == broker_party_id).all()
    audits = {a.id: a for a in s.query(Audit).filter(Audit.id.in_({f.audit_id for f in fs} or {0}))}
    events = _events(s, [f.id for f in fs])
    names = _names(s, {broker_party_id}, {a.contract_id for a in audits.values()},
                   {e.by_user_id for es in events.values() for e in es})
    carriers = {t.id: t.legal_name or t.tenant_name for t in
                s.query(Tenant).filter(Tenant.id.in_({f.tenant_id for f in fs} or {0}))}
    rows = []
    for f in sorted(fs, key=lambda f: (f.status == "closed", -_rank(f.severity), f.id)):
        a = audits.get(f.audit_id)
        v = finding_view(f, events.get(f.id, []), names, "broker", carriers.get(f.tenant_id), today)
        v.update({"audit": a.name if a else None, "audit_ref": audit_ref(a) if a else None,
                  "audit_type": TYPES.get(a.type) if a else None,
                  "contract": names["contract"].get(a.contract_id) if a else None,
                  "carrier": carriers.get(f.tenant_id)})
        rows.append(v)
    return {"ready": True, "findings": rows, "open": sum(1 for f in fs if f.status != "closed")}


def broker_finding(s, broker_party_id: int, finding_id: int) -> Finding:
    f = s.get(Finding, finding_id)
    if f is None or f.broker_party_id != broker_party_id:
        raise HTTPException(404, "That finding is not about your company.")
    return f


# ── for Partner Monitoring ─────────────────────────────────────────────────
def open_findings_by_broker(s, tenant_id: Optional[int]) -> dict[int, dict]:
    """{broker: {"open": n, "critical": n}} — zeros when migration 49 is not applied."""
    if missing_tables(s):
        return {}
    q = s.query(Finding).filter(Finding.status != "closed")
    if tenant_id is not None:
        q = q.filter(Finding.tenant_id == tenant_id)
    out: dict[int, dict] = {}
    for f in q:
        d = out.setdefault(f.broker_party_id, {"open": 0, "critical": 0})
        d["open"] += 1
        d["critical"] += f.severity == "critical"
    return out
