"""
audit_record_filter.py
──────────────────────
Task 49.1 — "who did what to THIS contract / policy / file", as a filter on
Audit Trails and Downloads & Views. Until now the only way in was the free-text
search, which matches the words on a row, not the record behind it: a download
of output file #279 names neither the contract nor the policy.

  ?record=contract:12      everything done to contract 12 — its own pages and
                           documents, and the files produced under it, with
                           the exception decisions made on them
  ?record=policy:P-1001    a policy opened in Policy Search, and every
                           exception decided on it
  ?record=file:march       every file whose name has "march" in it — received,
                           produced or uploaded — and what was done to it

It narrows, never widens: audit_feed applies it after the viewer's own scope
(scope_for), so it can only ever return fewer of the rows already allowed.
Sign-ins (auth_audit) are about a person, not a record, and drop out.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import cast, false, or_
from sqlalchemy.dialects.postgresql import JSONB

KINDS = ("contract", "policy", "file")
MAX_IDS = 400


@dataclass
class Record:
    kind: str
    value: str
    contract_id: Optional[int] = None
    export_ids: list[int] = field(default_factory=list)
    upload_ids: list[int] = field(default_factory=list)
    arrival_ids: list[int] = field(default_factory=list)


def parse(raw: Optional[str]) -> Optional[tuple[str, str]]:
    if not raw or ":" not in raw:
        return None
    kind, value = raw.split(":", 1)
    kind, value = kind.strip().lower(), value.strip()[:200]
    return (kind, value) if kind in KINDS and value else None


def resolve(s, raw: Optional[str]) -> Optional[Record]:
    """The ids behind one record, looked up once per request."""
    p = parse(raw)
    if p is None:
        return None
    from db import OutputExport, Upload
    from intake_models import FileArrival
    kind, value = p
    rec = Record(kind, value)
    if kind == "contract":
        if not value.isdigit():
            return None
        rec.contract_id = int(value)
        rec.export_ids = [r[0] for r in s.query(OutputExport.id).filter(OutputExport.contract_id == rec.contract_id)
                          .order_by(OutputExport.id.desc()).limit(MAX_IDS)]
    elif kind == "file":
        like = f"%{value}%"
        rec.export_ids = [r[0] for r in s.query(OutputExport.id).filter(OutputExport.filename.ilike(like))
                          .order_by(OutputExport.id.desc()).limit(MAX_IDS)]
        rec.upload_ids = [r[0] for r in s.query(Upload.id).filter(Upload.source_file.ilike(like))
                          .order_by(Upload.id.desc()).limit(MAX_IDS)]
        rec.arrival_ids = [int(r[0]) for r in s.query(FileArrival.id).filter(FileArrival.filename.ilike(like))
                           .order_by(FileArrival.id.desc()).limit(MAX_IDS)]
    return rec


def _alt(ids) -> str:
    return "|".join(str(int(i)) for i in ids)


def _file_paths(rec: Record) -> Optional[str]:
    """A regex for the paths and targets that name one of these files."""
    parts = []
    if rec.export_ids:
        parts.append(rf"(/export/downloads/|export:|/runs/)({_alt(rec.export_ids)})(/|$)")
    if rec.upload_ids:
        parts.append(rf"(/uploads/|upload:)({_alt(rec.upload_ids)})(/|$)")
    if rec.arrival_ids:
        parts.append(rf"(/intake/arrivals/|/arrivals/|arrival:)({_alt(rec.arrival_ids)})(/|$)")
    return "|".join(f"({p})" for p in parts) or None


def clause(model, rec: Record):
    """The rows of activity_events / access_log / auth_audit about `rec`."""
    from db import AccessLog, ActivityEvent
    if model is ActivityEvent:
        col, details = model.target, cast(model.details, JSONB)
    elif model is AccessLog:
        col, details = model.resource, None
    else:
        return false()
    ors = []
    files = _file_paths(rec)
    if rec.kind == "contract":
        cid = rec.contract_id
        ors += [col == f"contract:{cid}", col.op("~")(rf"/contracts/{cid}(/|$|\?)")]
        if details is not None:
            ors.append(details["contract_id"].astext == str(cid))
    elif rec.kind == "policy":
        like = f"%{rec.value}%"
        if model is AccessLog:
            ors.append(col.ilike(f"/policies/detail?policy=%{rec.value}%"))
        else:
            ors += [col.ilike(like), details["policy_number"].astext.ilike(like),
                    details["policy"].astext.ilike(like)]
    elif rec.kind == "file":
        if details is not None:
            ors += [details["filename"].astext.ilike(f"%{rec.value}%"),
                    details["file"].astext.ilike(f"%{rec.value}%")]
    if files:
        ors.append(col.op("~")(files))
    return or_(*ors) if ors else false()


def decision_clause(E, rec: Record):
    """Exception decisions: by the file they were made on, or the policy."""
    if rec.kind == "policy":
        return E.policy_number.ilike(f"%{rec.value}%")
    if rec.export_ids:
        return E.export_id.in_(rec.export_ids)
    return false()


def _resolved(s, f) -> Optional[Record]:
    """`f.record` looked up once per request, however many stores read it."""
    cached = getattr(f, "_record_resolved", None)
    if cached is None or cached[0] != f.record:
        cached = (f.record, resolve(s, f.record))
        f._record_resolved = cached
    return cached[1]


def where(s, model, f):
    """audit_feed._base_query's hook. A value this filter cannot read matches
    nothing — never everything."""
    rec = _resolved(s, f)
    return false() if rec is None else clause(model, rec)


def decision_where(s, E, f):
    """audit_feed._decision_query's hook."""
    rec = _resolved(s, f)
    return false() if rec is None else decision_clause(E, rec)


def choices(s, principal, q: str = "") -> dict:
    """The contracts this viewer may pick from — the same reach as Contract
    History, so the list never names a contract the viewer cannot see."""
    import audit_history as ah
    r = ah.reach(s, principal)
    rows, _pr = ah._contract_rows(s, r)
    names = sorted(({"id": c.id, "name": c.name or c.filename or f"Contract #{c.id}"} for c in rows),
                   key=lambda x: x["name"].lower())
    if q:
        names = [n for n in names if q.lower() in n["name"].lower()]
    return {"contracts": names[:500]}
