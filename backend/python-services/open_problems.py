"""Phase 2 · 44 — Exception Ownership & Workload.

One list of everything still waiting on somebody across a carrier's whole book,
so the carrier sees what is stuck, who has to act and what is overdue without
opening five screens. Carrier admin only. Read entirely from records that
already exist — nothing is written and no table is added:

  check       a check that failed on a broker's file and is not fixed yet
              (fix_performance.problems — MOPR 41's own definition of open)
  late_file   a bordereau that is overdue, delinquent, given more time, or
              never came (expected_submission, MOPR 39)
  dispute     the broker says a late mark is wrong; the carrier decides
              (expected_submission.case_state = in_dispute)
  held_file   a file that arrived and is waiting for a person to release or
              discard it (file_arrival, outcome held, no resolution)
  signature   an agreed contract waiting for a signature (contract lifecycle
              agreed/signed, contract_routes._whose_turn)

For every problem:
  OWNER     who has to act, as a COMPANY: the broker firm, or "Carrier" for the
            moves only the carrier can make (decide a dispute, release a held
            file, sign first). One login per broker firm, so never a person.
  DUE       the carrier's fix target (quality_setting, 5 days until changed —
            the number Exception Resolution uses) counted from the day the
            problem started; a late file is due on its calendar due date, or
            the date more time was agreed until.
  AGE       whole days since the problem started.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

CHECK, LATE_FILE, DISPUTE, HELD_FILE, SIGNATURE = (
    "check", "late_file", "dispute", "held_file", "signature")
TYPE_WORDS = {
    CHECK: "Failed check",
    LATE_FILE: "Late or missing file",
    DISPUTE: "Dispute",
    HELD_FILE: "File on hold",
    SIGNATURE: "Contract to sign",
}
TYPES = tuple(TYPE_WORDS)

CARRIER = "carrier"          # the owner key for the carrier's own moves

# expected_submission.case_state values that are still open (submission_case).
_LATE_STATES = {"overdue": "Overdue", "delinquent": "Delinquent",
                "extended": "More time given"}
# A period with no case opened yet but already late by the calendar (39.1).
_LATE_STATUSES = {"overdue": "Overdue", "never_arrived": "Never arrived"}
# Contract states that are waiting on a signature (contract_types / esign).
_SIGNING_STATES = ("agreed", "signed")

SORTS = ("age", "due")


def _as_date(v) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            v = v.astimezone(timezone.utc).replace(tzinfo=None)
        return v.date()
    return v if isinstance(v, date) else None


def _item(kind: str, *, started, due, status: str, owner: Optional[int | str],
          program_id, broker_party_id, title: str, detail: Optional[str] = None,
          link: Optional[str] = None, key: str) -> dict:
    """One problem. `owner` is a broker party id, or CARRIER."""
    return {"key": key, "type": kind, "type_label": TYPE_WORDS[kind],
            "status": status, "title": title, "detail": detail,
            "started_on": _as_date(started), "due_on": _as_date(due),
            "owner": owner, "program_id": program_id,
            "broker_party_id": broker_party_id, "link": link}


# --- the five sources ---------------------------------------------------------

def _checks(s, tid: int, target: int, program_id, broker_party_id) -> list[dict]:
    import fix_performance as fp
    out = []
    for p in fp.problems(s, tid, program_id=program_id, broker_party_id=broker_party_id):
        if p.get("fixed_at") is not None:
            continue
        found = p.get("found_at")
        where = " · ".join(str(x) for x in (
            p.get("policy_number") and f"Policy {p['policy_number']}",
            p.get("file"), p.get("period")) if x)
        b = p.get("broker_party_id")
        out.append(_item(
            CHECK, started=found,
            due=(_as_date(found) + timedelta(days=target)) if found else None,
            status="Waiting for a fix", owner=b or CARRIER,
            program_id=p.get("program_id"), broker_party_id=b,
            title=p.get("check") or p.get("message") or "Check failed",
            detail=where or None,
            link=f"/uploads/{p['export_id']}/exceptions" if p.get("export_id") else None,
            key=f"check:{p.get('export_id')}:{p.get('check_key')}:{p.get('sheet')}:"
                f"{p.get('row')}:{p.get('field')}:{p.get('policy_number')}"))
    return out


def _periods(s, tid: int, target: int, program_id, broker_party_id) -> list[dict]:
    from db import ExpectedSubmission, Program
    q = (s.query(ExpectedSubmission)
         .join(Program, Program.id == ExpectedSubmission.program_id)
         .filter(Program.tenant_id == tid))
    if program_id:
        q = q.filter(ExpectedSubmission.program_id == program_id)
    if broker_party_id:
        q = q.filter(ExpectedSubmission.broker_party_id == broker_party_id)
    q = q.filter((ExpectedSubmission.case_state.in_([*_LATE_STATES, "in_dispute"]))
                 | (ExpectedSubmission.case_state.is_(None)
                    & ExpectedSubmission.status.in_(list(_LATE_STATUSES))))
    out = []
    for e in q.all():
        b = e.broker_party_id
        if e.case_state == "in_dispute":
            raised = e.dispute_raised_on or e.case_since or e.due_date
            out.append(_item(
                DISPUTE, started=raised, due=_as_date(raised) + timedelta(days=target),
                status="Waiting for your decision", owner=CARRIER,
                program_id=e.program_id, broker_party_id=b,
                title=f"Late mark disputed for {e.period}",
                detail=(e.dispute_note or None), link="/bordereau-calendar",
                key=f"dispute:{e.id}"))
            continue
        status = (_LATE_STATES.get(e.case_state) if e.case_state
                  else _LATE_STATUSES.get(e.status)) or "Overdue"
        due = e.case_until if e.case_state == "extended" and e.case_until else e.due_date
        out.append(_item(
            LATE_FILE, started=e.due_date, due=due, status=status,
            owner=b or CARRIER, program_id=e.program_id, broker_party_id=b,
            title=f"Bordereau for {e.period} not received",
            link="/bordereau-calendar", key=f"late:{e.id}"))
    return out


def _held(s, tid: int, target: int, program_id, broker_party_id) -> list[dict]:
    from intake_models import FileArrival
    q = (s.query(FileArrival)
         .filter(FileArrival.tenant_id == tid, FileArrival.outcome == "held",
                 FileArrival.resolution.is_(None)))
    if program_id:
        q = q.filter(FileArrival.program_id == program_id)
    if broker_party_id:
        q = q.filter(FileArrival.matched_broker_party_id == broker_party_id)
    out = []
    for a in q.all():
        got = a.received_at
        out.append(_item(
            HELD_FILE, started=got,
            due=(_as_date(got) + timedelta(days=target)) if got else None,
            status="Waiting to be released", owner=CARRIER,
            program_id=a.program_id, broker_party_id=a.matched_broker_party_id,
            title=a.filename, detail=a.turned_away_reason or None,
            link="/files", key=f"held:{a.id}"))
    return out


def _signatures(s, tid: int, target: int, program_id, broker_party_id) -> list[dict]:
    import contract_routes as cr
    from db import Contract, ContractSignature, Program
    q = (s.query(Contract)
         .join(Program, Program.id == Contract.program_id)
         .filter(Program.tenant_id == tid, Contract.lifecycle.in_(_SIGNING_STATES)))
    if program_id:
        q = q.filter(Contract.program_id == program_id)
    if broker_party_id:
        q = q.filter(Contract.broker_party_id == broker_party_id)
    contracts = q.all()
    sigs: dict[int, list] = {}
    if contracts:
        for sg in (s.query(ContractSignature)
                   .filter(ContractSignature.contract_id.in_([c.id for c in contracts]))):
            sigs.setdefault(sg.contract_id, []).append(sg)
    out = []
    for c in contracts:
        if cr._effective_lifecycle(c) not in _SIGNING_STATES:
            continue
        turn = cr._whose_turn(c, cr._unsigned_sides(sigs.get(c.id, [])))
        if turn is None:
            continue
        b = c.broker_party_id
        owner = CARRIER if turn == "carrier" or not b else b
        unsigned = cr._unsigned_sides(sigs.get(c.id, []))
        status = ("Waiting to be made active" if not unsigned
                  else "Waiting for your signature" if turn == "carrier"
                  else "Waiting for the broker's signature")
        since = c.lifecycle_effective_date or c.submitted_at or c.created_at
        out.append(_item(
            SIGNATURE, started=since,
            due=(_as_date(since) + timedelta(days=target)) if since else None,
            status=status, owner=owner, program_id=c.program_id, broker_party_id=b,
            title=c.name or f"Contract {c.id}", link=f"/contracts/{c.id}",
            key=f"sign:{c.id}"))
    return out


SOURCES = {CHECK: _checks, LATE_FILE: _periods, DISPUTE: _periods,
           HELD_FILE: _held, SIGNATURE: _signatures}


def collect(s, tid: int, *, target: int, program_id: Optional[int] = None,
            broker_party_id: Optional[int] = None,
            kinds: Iterable[str] = TYPES) -> list[dict]:
    """Every open problem of the asked-for kinds. Each source runs once."""
    wanted = set(kinds) & set(TYPES)
    out: list[dict] = []
    ran = set()
    for kind in TYPES:
        fn = SOURCES[kind]
        if kind not in wanted or fn in ran:
            continue
        ran.add(fn)
        out.extend(fn(s, tid, target, program_id, broker_party_id))
    return [p for p in out if p["type"] in wanted]


# --- the list read back -------------------------------------------------------

def build(items: list[dict], *, today: date, owner: Optional[str] = None,
          kind: Optional[str] = None, overdue_only: bool = False,
          sort: str = "age", page: int = 1, page_size: int = 25) -> dict:
    """Filter, count and page the open problems. Pure — no database.

    The counts above the list (by type, workload by owner) are over the
    PROGRAMME / BROKER selection the problems were collected for, before the
    type, owner and overdue filters, so the tiles still say what each choice
    would show. The list itself is filtered first and paged last."""
    for p in items:
        started = p["started_on"]
        p["age_days"] = max((today - started).days, 0) if started else None
        p["overdue"] = bool(p["due_on"] and p["due_on"] < today)
        p["owner_key"] = CARRIER if p["owner"] == CARRIER else str(p["owner"])

    by_type = {k: {"type": k, "label": TYPE_WORDS[k], "open": 0, "overdue": 0} for k in TYPES}
    workload: dict[str, dict] = {}
    for p in items:
        t = by_type[p["type"]]
        t["open"] += 1
        t["overdue"] += p["overdue"]
        w = workload.setdefault(p["owner_key"], {
            "owner": p["owner_key"], "open": 0, "overdue": 0, "oldest_days": None,
            "by_type": {k: 0 for k in TYPES}})
        w["open"] += 1
        w["overdue"] += p["overdue"]
        w["by_type"][p["type"]] += 1
        if p["age_days"] is not None:
            w["oldest_days"] = max(w["oldest_days"] or 0, p["age_days"])

    shown = [p for p in items
             if (not kind or p["type"] == kind)
             and (not owner or p["owner_key"] == owner)
             and (not overdue_only or p["overdue"])]
    if sort == "due":
        shown.sort(key=lambda p: (p["due_on"] is None, p["due_on"] or today, -(p["age_days"] or 0)))
    else:
        shown.sort(key=lambda p: (-(p["age_days"] or 0), p["due_on"] or today))
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 25), 100))
    cut = shown[(page - 1) * page_size: page * page_size]

    def _iso(v):
        return v.isoformat() if isinstance(v, date) else v

    return {
        "as_of": today.isoformat(),
        "total_open": len(items),
        "total_overdue": sum(p["overdue"] for p in items),
        "by_type": list(by_type.values()),
        "workload": sorted(workload.values(),
                           key=lambda w: (-w["overdue"], -w["open"], w["owner"])),
        "page": page, "page_size": page_size, "total": len(shown),
        "items": [{**{k: v for k, v in p.items() if k not in ("started_on", "due_on")},
                   "started_on": _iso(p["started_on"]), "due_on": _iso(p["due_on"])}
                  for p in cut],
    }
