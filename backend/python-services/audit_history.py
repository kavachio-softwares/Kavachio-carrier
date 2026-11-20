"""
audit_history.py
────────────────
The Audit Logs tabs beyond the trail itself (tasks 49.1, 49.2, 49.3, 51.2),
all read from what is already stored — no tables of their own:

  summary()           the trail's tiles: activities, people, views, downloads, rule changes
  access()            Downloads & Views — every view and download (audit_feed, views on)
  submissions()       Partner Submission History — every file a broker owed, across programmes
  submission_timeline()  what happened to one owed file: reminders, chases, case steps, receipt
  contracts()         Contract History — every step on every contract, newest first
  contract_timeline() one contract's steps, oldest first
  versions()          File Versions — every version of every file a broker sent

WHO SEES WHAT is the same as everywhere else: Kavachio everything (or one
carrier, by `mga`), a carrier its own programmes and their brokers, a broker
only its own — never another broker's (audit_feed.viewer_for decides the seat).
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, or_

import audit_feed as af

# ── who may be shown ───────────────────────────────────────────────────────


class Reach:
    """The carriers and the broker one viewer may read. `tenants` None = all."""

    def __init__(self, v: af.Viewer, tenants: Optional[set[int]], broker: Optional[int]):
        self.v, self.tenants, self.broker = v, tenants, broker

    def filter(self, q, tenant_col, broker_col):
        if self.tenants is not None:
            q = q.filter(tenant_col.in_(sorted(self.tenants) or [0]))
        if self.broker is not None:
            q = q.filter(broker_col == self.broker)
        return q

    def allows(self, tenant_id, broker_id) -> bool:
        if self.tenants is not None and tenant_id not in self.tenants:
            return False
        return self.broker is None or broker_id == self.broker


def chosen_mga(mga: Optional[str]) -> Optional[str]:
    """The carrier code a screen sent, or None when none was really picked.
    The frontend sends its placeholder "default" (auth.currentMga) for a
    Kavachio admin, who has no carrier of their own: that means every carrier,
    not a carrier called "default"."""
    m = (mga or "").strip()
    return None if m in ("", "default") else m


def reach(s, principal, mga: Optional[str] = None) -> Reach:
    v = af.viewer_for(s, principal)
    mga = chosen_mga(mga)
    if v.is_platform:
        if mga:
            from app_routes import resolve_tenant_id
            return Reach(v, {resolve_tenant_id(s, principal, mga)}, None)
        return Reach(v, None, None)
    if v.is_broker:
        if v.broker_party_id is None:
            raise HTTPException(403, "Your login is not linked to a broker.")
        return Reach(v, None, v.broker_party_id)
    return Reach(v, {v.tenant_id} if v.tenant_id is not None else set(), None)


def _names(s, programme_ids=(), party_ids=(), tenant_ids=()) -> dict:
    from db import Party, Program, Tenant
    pg = {i for i in programme_ids if i}
    pt = {i for i in party_ids if i}
    tn = {i for i in tenant_ids if i}
    return {
        "programme": dict(s.query(Program.id, Program.name).filter(Program.id.in_(pg))) if pg else {},
        "party": dict(s.query(Party.id, Party.legal_name).filter(Party.id.in_(pt))) if pt else {},
        "carrier": {t.id: (t.legal_name or t.tenant_name) for t in
                    s.query(Tenant).filter(Tenant.id.in_(tn))} if tn else {},
    }


def _iso(x) -> Optional[str]:
    if x is None:
        return None
    if isinstance(x, dt.datetime):
        if x.tzinfo is not None:
            x = x.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return x.isoformat() + "Z"
    return x.isoformat()


def _when(x) -> dt.datetime:
    """Any stored date or time, as a naive UTC instant — so they sort together."""
    if x is None:
        return dt.datetime.min
    if isinstance(x, dt.datetime):
        return x.astimezone(dt.timezone.utc).replace(tzinfo=None) if x.tzinfo else x
    return dt.datetime.combine(x, dt.time())


def _page(items: list, page: int, page_size: int) -> dict:
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    a = (page - 1) * page_size
    return {"items": items[a:a + page_size], "total": len(items), "page": page, "page_size": page_size}


def _day(v: Optional[str]) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(v[:10]) if v else None
    except ValueError:
        raise HTTPException(400, "Pick real dates.")


class People:
    """A user id → the name this viewer may see: a person in the viewer's own
    organisation (or anyone, for Kavachio) by name; someone at the other side
    by their company — the carrier never sees broker staff, nor the broker the
    carrier's."""

    def __init__(self, s, v: af.Viewer):
        self.s, self.v, self._cache = s, v, {}

    def name(self, user_id: Optional[int], fallback: str = "Kavachio · Automatic") -> str:
        if not user_id:
            return fallback
        if user_id in self._cache:
            return self._cache[user_id]
        from db import AppUser, Party, Tenant
        u = self.s.get(AppUser, user_id)
        if u is None:
            out = "A former user"
        elif self.v.is_platform:
            out = u.full_name or u.email
        elif u.broker_party_id:
            mine = self.v.is_broker and self.v.broker_party_id == u.broker_party_id
            p = None if mine else self.s.get(Party, u.broker_party_id)
            out = (u.full_name or u.email) if mine else (p.legal_name if p else "The broker")
        else:
            mine = self.v.is_carrier and self.v.tenant_id == u.tenant_id
            t = None if mine else (self.s.get(Tenant, u.tenant_id) if u.tenant_id else None)
            out = (u.full_name or u.email) if mine else ((t.legal_name or t.tenant_name) if t else "The carrier")
        self._cache[user_id] = out
        return out


# ── the trail's tiles ──────────────────────────────────────────────────────
def summary(s, principal, f: af.Filters, programme: Optional[int] = None, status: str = "",
            tz: int = 0) -> dict:
    """The numbers above the trail, for the same dates and filters. The total
    follows every filter; the other four follow dates, person, Area and Action."""
    from db import AccessLog, ActivityEvent
    import audit_one
    v = af.viewer_for(s, principal)
    sc = af.scope_for(s, v)
    total = audit_one.page(s, v, f, 1, 1, programme=programme, status=status, tz=tz)["total"]
    q_ids = af._search_targets(s, f.q) if f.q else ([], [])
    people: set = set()
    rule_changes = 0
    qa = af._base_query(s, ActivityEvent, sc, f, q_ids)
    if qa is not None:
        people |= {r[0] for r in qa.with_entities(ActivityEvent.actor_user_id).distinct() if r[0]}
        rule_changes = qa.filter(ActivityEvent.action.ilike("%rule%")).count()
    views = downloads = 0
    acc = af._base_query(s, AccessLog, sc, f, q_ids)
    if acc is not None:
        people |= {r[0] for r in acc.with_entities(AccessLog.user_id).distinct() if r[0]}
        down = acc.filter(AccessLog.action == "download",
                          AccessLog.resource.op("~")(af.DOWNLOAD_RESOURCES))
        downloads = down.count()
        views = acc.count() - downloads
    return {"activities": total, "people": len(people), "viewed": views,
            "downloaded": downloads, "rule_changes": rule_changes}


# ── Downloads & Views ──────────────────────────────────────────────────────
_CONTRACT_REF = re.compile(r"Contract #(\d+)")
_EXPORT_REF = re.compile(r"Output BDX #(\d+)")


def access(s, principal, f: af.Filters, kind: str, page: int, page_size: int) -> dict:
    """Every view and download in the dates picked, newest first, with the
    contract or file it touched named rather than numbered."""
    from db import Contract, OutputExport
    f.views, f.access_kind = True, kind if kind in ("view", "download") else ""
    f.categories = ("access",)
    v = af.viewer_for(s, principal)
    out = af.page(s, v, f, page, page_size)
    cids = {int(m.group(1)) for r in out["items"] for m in [_CONTRACT_REF.search(r.get("target") or "")] if m}
    eids = {int(m.group(1)) for r in out["items"] for m in [_EXPORT_REF.search(r.get("target") or "")] if m}
    cn = {c.id: c.name or c.filename or f"Contract #{c.id}"
          for c in s.query(Contract).filter(Contract.id.in_(cids))} if cids else {}
    en = dict(s.query(OutputExport.id, OutputExport.filename).filter(OutputExport.id.in_(eids))) if eids else {}
    for r in out["items"]:
        t = r.get("target") or ""
        t = _CONTRACT_REF.sub(lambda m: cn.get(int(m.group(1)), m.group(0)), t)
        t = _EXPORT_REF.sub(lambda m: en.get(int(m.group(1))) or m.group(0), t)
        r["target"] = t
        r["kind"] = "Downloaded" if r.get("status") == "Downloaded" else "Viewed"
    # The tiles for the same dates, whatever kind is picked.
    from db import AccessLog
    sc = af.scope_for(s, v)
    base = af.Filters(since=f.since, until=f.until, actor_user_ids=f.actor_user_ids,
                      actor_broker_ids=f.actor_broker_ids, actor_system=f.actor_system, q=f.q,
                      record=f.record)
    q = af._base_query(s, AccessLog, sc, base, af._search_targets(s, f.q) if f.q else ([], []))
    if q is None:
        out["counts"] = {"viewed": 0, "downloaded": 0, "people": 0}
    else:
        down = q.filter(AccessLog.action == "download", AccessLog.resource.op("~")(af.DOWNLOAD_RESOURCES)).count()
        out["counts"] = {"viewed": q.count() - down, "downloaded": down,
                         "people": q.with_entities(AccessLog.user_id).filter(AccessLog.user_id.isnot(None))
                         .distinct().count()}
    return out


# ── Partner Submission History ─────────────────────────────────────────────
# derive_status → the words on screen.
SUBMISSION_WORDS = {"on_time": "On time", "received_late": "Late", "overdue": "Missing",
                    "never_arrived": "Missing", "due_today": "Due today",
                    "due_soon": "Not due yet", "scheduled": "Not due yet", "waived": "No file needed"}
SUBMISSION_TONE = {"On time": "ok", "Late": "warn", "Missing": "bad", "Due today": "info",
                   "Not due yet": "muted", "No file needed": "muted"}
WAIVED = ("waived", "accepted")


def _state(e, today: dt.date) -> str:
    from submission_calendar import derive_status
    if (e.case_resolution or "") in WAIVED and not e.received_at:
        return "waived"
    return derive_status(e.due_date, today, 0, e.received_at, e.closes_on)


def submissions(s, principal, *, mga=None, broker: Optional[int] = None, programme: Optional[int] = None,
                period: Optional[str] = None, since: Optional[str] = None, until: Optional[str] = None,
                status: Optional[str] = None, page: int = 1, page_size: int = 50,
                today: Optional[dt.date] = None) -> dict:
    """Every file a broker owed in the dates picked (by due date), across all
    programmes, with how it ended — and the totals for the tiles."""
    from db import ExpectedSubmission as E
    r = reach(s, principal, mga)
    today = today or dt.datetime.utcnow().date()
    q = r.filter(s.query(E), E.tenant_id, E.broker_party_id)
    if broker:
        q = q.filter(E.broker_party_id == broker)
    if programme:
        q = q.filter(E.program_id == programme)
    if period:
        q = q.filter(E.period == period)
    a, b = _day(since), _day(until)
    if a:
        q = q.filter(E.due_date >= a)
    if b:
        q = q.filter(E.due_date <= b)
    rows = q.order_by(E.due_date.desc(), E.id.desc()).limit(5000).all()
    names = _names(s, {e.program_id for e in rows}, {e.broker_party_id for e in rows})
    items, counts = [], {"expected": 0, "received": 0, "late": 0, "missing": 0}
    for e in rows:
        st = _state(e, today)
        words = SUBMISSION_WORDS.get(st, st)
        due = st not in ("scheduled", "due_soon", "due_today", "waived")
        counts["expected"] += due
        counts["received"] += st in ("on_time", "received_late")
        counts["late"] += st == "received_late"
        counts["missing"] += st in ("overdue", "never_arrived")
        if status and words != status:
            continue
        items.append({
            "id": e.id, "programme_id": e.program_id, "broker_id": e.broker_party_id,
            "programme": names["programme"].get(e.program_id) or f"Programme #{e.program_id}",
            "broker": names["party"].get(e.broker_party_id) or "—",
            "period": e.period, "due_on": _iso(e.due_date), "received_on": _iso(e.received_at),
            "status": words, "tone": SUBMISSION_TONE.get(words, "muted"),
            "days_late": max(((e.received_at or today) - e.due_date).days, 0)
            if st in ("received_late", "overdue", "never_arrived") else 0,
            "reminders": int(e.chase_count or 0)})
    out = _page(items, page, page_size)
    out["counts"] = counts
    return out


REMINDER_WORDS = {"submission_due_soon": "Reminder sent — the file is due soon",
                  "submission_due_today": "Reminder sent — the file is due today",
                  "submission_overdue": "Reminder sent — the file is overdue"}


def submission_timeline(s, principal, expected_id: int, today: Optional[dt.date] = None) -> dict:
    """Everything that happened to one owed file, oldest first."""
    from db import ActivityEvent, ExpectedSubmission
    from intake_models import FileArrival
    import submission_case as sc_
    e = s.get(ExpectedSubmission, expected_id)
    r = reach(s, principal)
    if e is None or not r.allows(e.tenant_id, e.broker_party_id):
        raise HTTPException(404, "That file is not one of yours.")
    today = today or dt.datetime.utcnow().date()
    people = People(s, r.v)
    steps: list[dict] = [{"at": _iso(e.created_at), "_k": _when(e.created_at), "kind": "expected",
                          "title": f"File expected for {af_period(e.period)}",
                          "detail": f"Due on {e.due_date:%d %b %Y}.", "by": "Kavachio · Automatic"}]
    if e.due_date <= today:
        steps.append({"at": _iso(e.due_date), "_k": _when(e.due_date) + dt.timedelta(hours=23, minutes=59),
                      "kind": "due", "title": "Due date reached",
                      "detail": None if e.received_at and e.received_at <= e.due_date
                      else "The file had not arrived yet.", "by": "Kavachio · Automatic"})
    acts = (s.query(ActivityEvent)
            .filter(ActivityEvent.tenant_id == e.tenant_id,
                    ActivityEvent.target == f"program:{e.program_id}",
                    ActivityEvent.action.in_(list(REMINDER_WORDS) + ["submission_chased"]))
            .order_by(ActivityEvent.created_at.asc()).all())
    case_steps = sc_.history(s, e.id, label=lambda uid: people.name(uid))
    # Since 49.2 a chase is also written into the case itself; read it from
    # there, and from the general log only for chases made before that.
    case_chases = [_when(_parse(h.get("at"))) for h in case_steps if h.get("kind") == "chased"]
    for a in acts:
        d = a.details if isinstance(a.details, dict) else {}
        if d.get("period") != e.period:
            continue
        if d.get("broker_party_id") not in (None, e.broker_party_id):
            continue
        if a.action == "submission_chased" and any(
                abs((_when(a.created_at) - c).total_seconds()) < 60 for c in case_chases):
            continue
        if a.action == "submission_chased":
            steps.append({"at": _iso(a.created_at), "_k": _when(a.created_at), "kind": "chase",
                          "title": "Broker chased for the file",
                          "detail": (f"Message: “{d['note']}”" if d.get("note") else None),
                          "by": people.name(a.actor_user_id)})
        else:
            steps.append({"at": _iso(a.created_at), "_k": _when(a.created_at), "kind": "reminder",
                          "title": REMINDER_WORDS[a.action], "detail": None, "by": "Kavachio · Automatic"})
    for h in case_steps:
        if h.get("kind") == "chased":
            steps.append({"at": h.get("at"), "_k": _when(_parse(h.get("at"))), "kind": "chase",
                          "title": h.get("kind_label"),
                          "detail": f"Message: “{h['note']}”" if h.get("note") else None,
                          "by": "Kavachio · Automatic" if h.get("by") == "Automatic" else h.get("by")})
            continue
        steps.append({"at": h.get("at"), "_k": _when(_parse(h.get("at"))), "kind": "case",
                      "title": h.get("kind_label") or h.get("kind"),
                      "detail": " · ".join(x for x in (h.get("reason_label"), h.get("note")) if x) or None,
                      "by": "Kavachio · Automatic" if h.get("by") == "Automatic" else h.get("by")})
    files = (s.query(FileArrival)
             .filter(FileArrival.tenant_id == e.tenant_id, FileArrival.program_id == e.program_id,
                     FileArrival.reporting_period == e.period,
                     or_(FileArrival.matched_broker_party_id == e.broker_party_id,
                         FileArrival.matched_broker_party_id.is_(None)))
             .order_by(FileArrival.received_at.asc()).all())
    for fa in files:
        steps.append({"at": _iso(fa.received_at), "_k": _when(fa.received_at), "kind": "received",
                      "title": f"File received{f' — version {fa.version_no}' if fa.version_no else ''}",
                      "detail": fa.filename, "by": people.name(fa.submitted_by_user_id, "The broker")})
    if not files and e.received_at:
        steps.append({"at": _iso(e.received_at), "_k": _when(e.received_at), "kind": "received",
                      "title": "File received", "detail": None, "by": "The broker"})
    steps.sort(key=lambda x: x.pop("_k"))
    names = _names(s, [e.program_id], [e.broker_party_id])
    st = _state(e, today)
    return {"id": e.id, "programme": names["programme"].get(e.program_id),
            "broker": names["party"].get(e.broker_party_id), "period": e.period,
            "due_on": _iso(e.due_date), "status": SUBMISSION_WORDS.get(st, st), "steps": steps}


def _parse(v):
    if not v:
        return None
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def af_period(p: Optional[str]) -> str:
    """'2026-10' → 'Oct 2026'; anything else as it is."""
    try:
        return dt.date(int(p[:4]), int(p[5:7]), 1).strftime("%b %Y") if p and re.fullmatch(r"\d{4}-\d{2}", p) else (p or "")
    except ValueError:
        return p or ""


# ── Contract History ───────────────────────────────────────────────────────
# A step → (what it is in words, what kind, the contract's status after it).
# None as the status: the step does not move the contract's own status.
STEPS = {
    # Raised in Kavachio or uploaded already signed — the same first step.
    "raised": ("Contract added", "Draft", None),
    "submitted": ("Sent to the carrier for approval", "Review", "Waiting for approval"),
    "sent_for_review": ("Sent to the broker to review", "Review", "In review"),
    "sent_back": ("Sent back for changes", "Review", "Draft"),
    "changes_requested": ("Broker asked for changes", "Review", "Changes requested"),
    "terms_agreed": ("Terms agreed", "Review", "Agreed"),
    "review_skipped": ("Agreed without a review", "Review", "Agreed"),
    "carrier_accepted": ("Accepted by the carrier", "Signing", "Accepted"),
    "signed_submitted": ("Signed", "Signing", "Signed"),
    "period_corrected": ("Contract dates corrected", "Change", None),
    "period_correction_undone": ("Date correction undone", "Change", None),
    "mid_term_sent": ("Mid-term change sent to the broker", "Amendment", None),
    "mid_term_uploaded": ("Mid-term change uploaded", "Amendment", None),
    "mid_term_changes_requested": ("Broker asked for changes to a mid-term change", "Amendment", None),
    "mid_term_agreed": ("Mid-term change agreed", "Amendment", None),
    "mid_term_signed": ("Mid-term change signed", "Amendment", None),
    "mid_term_accepted": ("Mid-term change accepted", "Amendment", None),
    "mid_term_live": ("Mid-term change now in force", "Amendment", None),
    "mid_term_withdrawn": ("Mid-term change withdrawn", "Amendment", None),
    # From the general log (no row of their own on the contract).
    "contract_edited": ("Draft changed", "Draft", None),
    "contract_activated": ("Made active", "Status", "Active"),
    "contract_terminated": ("Ended early (terminated)", "Status", "Terminated"),
    "contract_renewed": ("Renewal started", "Renewal", None),
    "contract_document_attached": ("Document added", "Change", None),
    "contract_endorsed": ("Endorsement added", "Amendment", None),
    "contract_checks_bound": ("Checks switched on", "Status", None),
}
_FROM_LOG = [k for k in STEPS if k.startswith("contract_")]
_CONTRACT_PATH = re.compile(r"^/contracts/(\d+)(?:/|$)")


def _contract_rows(s, r: Reach, *, broker=None, programme=None, contract=None, status=None):
    from db import Contract
    import programme_report as pr
    q = r.filter(s.query(Contract), Contract.tenant_id, Contract.broker_party_id)
    if broker:
        q = q.filter(Contract.broker_party_id == broker)
    if programme:
        q = q.filter(Contract.program_id == programme)
    if contract:
        q = q.filter(Contract.id == contract)
    rows = q.all()
    if status:
        rows = [c for c in rows if _status_words(c.lifecycle or c.status) == status]
    return rows, pr


def _status_words(v: Optional[str]) -> str:
    v = (v or "").strip()
    return (v[0].upper() + v[1:]).replace("_", " ") if v else "—"


def _steps_of(s, contracts: list, people: People) -> dict[int, list[dict]]:
    """Every step of every contract given, oldest first, with the status
    before and after each."""
    from db import ActivityEvent, ContractApproval
    ids = [c.id for c in contracts]
    by: dict[int, list[dict]] = {c.id: [{"_k": _when(c.created_at), "action": "raised",
                                         "user": c.submitted_by_user_id, "note": None}]
                                 for c in contracts}
    if not ids:
        return {}
    for a in s.query(ContractApproval).filter(ContractApproval.contract_id.in_(ids)):
        by[a.contract_id].append({"_k": _when(a.acted_at), "action": a.action,
                                  "user": a.acted_by_user_id, "note": a.note})
    tenants = {c.tenant_id for c in contracts if c.tenant_id}
    q = s.query(ActivityEvent).filter(ActivityEvent.action.in_(_FROM_LOG),
                                      ActivityEvent.target.like("/contracts/%"))
    if tenants:
        q = q.filter(or_(ActivityEvent.tenant_id.in_(sorted(tenants)), ActivityEvent.tenant_id.is_(None)))
    for ev in q:
        m = _CONTRACT_PATH.match(ev.target or "")
        if m and int(m.group(1)) in by:
            by[int(m.group(1))].append({"_k": _when(ev.created_at), "action": ev.action,
                                        "user": ev.actor_user_id, "note": None})
    # Task 49.3: each "Draft changed" opens the before / after recorded for
    # that save (change_history.py) — from the day that recording began.
    import change_history as chist
    edits = chist.edits_near(s, ids)
    out: dict[int, list[dict]] = {}
    lifecycle = {c.id: (getattr(c, "lifecycle", None) or "").lower() for c in contracts}
    for cid, steps in by.items():
        steps.sort(key=lambda x: x["_k"])
        now = None
        rows = []
        # Added in Kavachio, it starts as a draft; uploaded already signed and
        # never sent anywhere since, it was active from the start.
        moved = any(STEPS.get(x["action"], (None, None, None))[2] for x in steps)
        first = "Active" if lifecycle.get(cid) == "active" and not moved else "Draft"
        for st in steps:
            words, kind, after = STEPS.get(st["action"], (_status_words(st["action"]), "Change", None))
            if st["action"] == "raised":
                after = first
            before = now
            if after:
                now = after
            rows.append({"at": _iso(st["_k"]) if st["_k"] != dt.datetime.min else None, "_k": st["_k"],
                         "action": st["action"], "title": words, "kind": kind,
                         "before": before if after else None, "after": after,
                         "note": st["note"], "by": people.name(st["user"], "—") if st["user"] else
                         ("—" if st["action"] == "raised" else "Kavachio · Automatic"),
                         "change_id": chist.match_edit(edits.get(cid), st["_k"], st["user"])
                         if st["action"] == "contract_edited" else None})
        out[cid] = rows
    return out


def contracts(s, principal, *, mga=None, broker=None, programme=None, contract=None, status=None,
              since=None, until=None, page: int = 1, page_size: int = 50) -> dict:
    """Every step on the contracts picked, in the dates picked, newest first."""
    from db import Contract, ContractApproval
    r = reach(s, principal, mga)
    rows, pr = _contract_rows(s, r, broker=broker, programme=programme, contract=contract, status=status)
    rows = [c for c in rows if pr._with_broker(c)]
    people = People(s, r.v)
    steps = _steps_of(s, rows, people)
    a, b = _day(since), _day(until)
    lo = dt.datetime.combine(a, dt.time()) if a else None
    hi = dt.datetime.combine(b, dt.time()) + dt.timedelta(days=1) if b else None
    names = _names(s, {c.program_id for c in rows}, {c.broker_party_id for c in rows})
    by_id = {c.id: c for c in rows}
    items = []
    for cid, ss in steps.items():
        c = by_id[cid]
        for st in ss:
            k = st["_k"]
            if (lo and k < lo) or (hi and k >= hi):
                continue
            items.append({**{x: st[x] for x in st if x != "_k"}, "_k": k, "contract_id": cid,
                          "contract": c.name or c.filename or f"Contract #{cid}",
                          "programme": names["programme"].get(c.program_id) or "—",
                          "broker": names["party"].get(c.broker_party_id) or "—"})
    items.sort(key=lambda x: x.pop("_k"), reverse=True)
    ids = list(by_id)
    mid = {x[0] for x in s.query(ContractApproval.contract_id).filter(
        ContractApproval.contract_id.in_(ids or [0]), ContractApproval.action == "mid_term_live")}
    out = _page(items, page, page_size)
    out["counts"] = {"contracts": len(rows),
                     "active": sum(1 for c in rows if (c.lifecycle or c.status or "").lower() in ("active", "in_force", "bound")),
                     "changed_mid_term": len(mid),
                     "renewed": sum(1 for c in rows if c.renews_contract_id)}
    out["choices"] = {"contracts": sorted(({"id": c.id, "name": c.name or c.filename or f"Contract #{c.id}"}
                                           for c in rows), key=lambda x: x["name"].lower()),
                      "statuses": sorted({_status_words(c.lifecycle or c.status) for c in rows})}
    return out


def contract_timeline(s, principal, contract_id: int) -> dict:
    from db import Contract
    c = s.get(Contract, contract_id)
    r = reach(s, principal)
    if c is None or not r.allows(c.tenant_id, c.broker_party_id):
        raise HTTPException(404, "That contract is not one of yours.")
    steps = _steps_of(s, [c], People(s, r.v))[c.id]
    for st in steps:
        st.pop("_k", None)
    names = _names(s, [c.program_id], [c.broker_party_id])
    renews = s.get(Contract, c.renews_contract_id) if c.renews_contract_id else None
    renewed_by = s.query(Contract).filter(Contract.renews_contract_id == c.id).first()
    return {"id": c.id, "name": c.name or c.filename or f"Contract #{c.id}",
            "programme": names["programme"].get(c.program_id), "broker": names["party"].get(c.broker_party_id),
            "status": _status_words(c.lifecycle or c.status),
            "starts": _iso(c.inception_dt), "ends": _iso(getattr(c, "expiry_dt", None)),
            "renews": renews and {"id": renews.id, "name": renews.name or renews.filename or f"Contract #{renews.id}"},
            "renewed_by": renewed_by and {"id": renewed_by.id,
                                          "name": renewed_by.name or renewed_by.filename or f"Contract #{renewed_by.id}"},
            "steps": steps}


# ── File Versions ──────────────────────────────────────────────────────────
def versions(s, principal, *, mga=None, broker=None, programme=None, period=None,
             page: int = 1, page_size: int = 50) -> dict:
    """Every version of every file a broker sent, newest first. A resend for
    the same month, or a correction, is the next version; earlier ones stay."""
    from db import OutputExport
    from intake_models import FileArrival
    import partner_performance as pp
    import submission_service as ss_
    r = reach(s, principal, mga)
    q = r.filter(s.query(FileArrival).filter(FileArrival.submission_ref.isnot(None)),
                 FileArrival.tenant_id, FileArrival.matched_broker_party_id)
    if broker:
        q = q.filter(FileArrival.matched_broker_party_id == broker)
    if programme:
        q = q.filter(FileArrival.program_id == programme)
    if period:
        q = q.filter(FileArrival.reporting_period == period)
    files = q.order_by(FileArrival.received_at.desc()).limit(5000).all()
    refs = {f.submission_ref for f in files}
    fixes = (r.filter(s.query(OutputExport).filter(OutputExport.submission_ref.in_(refs),
                                                   OutputExport.version_status.isnot(None)),
                      OutputExport.tenant_id, OutputExport.broker_party_id).all()) if refs else []
    thread = {f.submission_ref: f for f in sorted(files, key=lambda f: f.version_no or 0)}  # the latest file
    rows = []
    for f in files:
        rows.append({"ref": f.submission_ref, "version": f.version_no or 1, "filename": f.filename,
                     "program_id": f.program_id, "broker_id": f.matched_broker_party_id,
                     "period": f.reporting_period, "at": f.received_at,
                     "how": pp.CHANNEL_LABEL.get(f.channel or "", _status_words(f.channel)),
                     "state": ss_.STATUS_WORDS.get(f.version_status or "", None),
                     "export_id": f.run_export_id})
    for x in fixes:
        first = thread.get(x.submission_ref)
        rows.append({"ref": x.submission_ref, "version": x.version_no or 1, "filename": x.filename,
                     "program_id": x.program_id or (first.program_id if first else None),
                     "broker_id": x.broker_party_id, "period": first.reporting_period if first else None,
                     "at": x.created_at, "how": "Corrected online",
                     "state": ss_.STATUS_WORDS.get(x.version_status or "", None), "export_id": x.id})
    latest: dict = {}
    for row in rows:
        latest[row["ref"]] = max(latest.get(row["ref"], 0), row["version"])
    names = _names(s, {x["program_id"] for x in rows}, {x["broker_id"] for x in rows})
    rows.sort(key=lambda x: _when(x["at"]), reverse=True)
    items = [{**{k: v for k, v in x.items() if k not in ("at",)}, "received_at": _iso(x["at"]),
              "programme": names["programme"].get(x["program_id"]) or "—",
              "broker": names["party"].get(x["broker_id"]) or "—",
              "current": x["version"] == latest[x["ref"]],
              "versions": latest[x["ref"]]} for x in rows]
    out = _page(items, page, page_size)
    out["counts"] = {"files": len(latest), "versions": len(items),
                     "resent": sum(1 for v in latest.values() if v > 1)}
    return out


# ── the dropdowns ──────────────────────────────────────────────────────────
def choices(s, principal, mga=None) -> dict:
    """The programmes and brokers this viewer may pick from."""
    from db import Program, ProgramBroker
    r = reach(s, principal, mga)
    pb = s.query(ProgramBroker.program_id, ProgramBroker.broker_party_id).join(
        Program, Program.id == ProgramBroker.program_id)
    if r.tenants is not None:
        pb = pb.filter(Program.tenant_id.in_(sorted(r.tenants) or [0]))
    if r.broker is not None:
        pb = pb.filter(ProgramBroker.broker_party_id == r.broker)
    pairs = pb.limit(5000).all()
    pq = s.query(Program.id, Program.name)
    if r.tenants is not None:
        pq = pq.filter(Program.tenant_id.in_(sorted(r.tenants) or [0]))
    if r.broker is not None:
        pq = pq.filter(Program.id.in_({p for p, _b in pairs} or {0}))
    names = _names(s, (), {b for _p, b in pairs})
    by_name = lambda d: sorted(({"id": k, "name": v or f"#{k}"} for k, v in d.items()),  # noqa: E731
                               key=lambda x: x["name"].lower())
    return {"programmes": by_name(dict(pq.limit(2000).all())),
            "brokers": [] if r.broker is not None else by_name(names["party"])}


# ── Partner Monitoring (54.1) ──────────────────────────────────────────────
MONITOR_MONTHS = 6
LATE_HIGH = 3          # late files in the window that make a broker High risk
RISK_RULE = (f"High: an open critical finding, a missing file, or {LATE_HIGH} or more late files "
             f"in the last {MONITOR_MONTHS} months. Medium: any open finding, or 1–{LATE_HIGH - 1} late files. "
             "Low: everything else.")


def risk_of(late: int, missing: int, open_findings: int, critical: int) -> tuple[str, list[str]]:
    """(High | Medium | Low, the reasons in words)."""
    why = []
    if critical:
        why.append(f"{critical} open critical {'finding' if critical == 1 else 'findings'}")
    if missing:
        why.append(f"{missing} missing {'file' if missing == 1 else 'files'}")
    if late:
        why.append(f"{late} late {'file' if late == 1 else 'files'}")
    if open_findings - critical > 0:
        n = open_findings - critical
        why.append(f"{n} open {'finding' if n == 1 else 'findings'}")
    if critical or missing or late >= LATE_HIGH:
        return "High", why
    if open_findings or late:
        return "Medium", why
    return "Low", why or ["Nothing late, missing or open"]


# The kind of company in a programme's "broker" place (party_type). A claims
# handler is a TPA, and sits in the same place (hierarchy_routes).
PARTNER_TYPES = {"broker": "Broker", "tpa": "Claims handler (TPA)", "mga": "MGA", "mgu": "MGU",
                 "agency": "Agency", "insurer": "Insurer", "carrier": "Carrier", "reinsurer": "Reinsurer",
                 "vendor": "Vendor", "individual": "Individual", "other": "Other"}


def _monitor_rows(s, r: Reach, *, broker=None, programme=None, today=None) -> list[dict]:
    from db import ExpectedSubmission as E, Program, ProgramBroker
    from intake_models import FileArrival
    import audit_management as am
    today = today or dt.datetime.utcnow().date()
    since = (dt.datetime.combine(today, dt.time()) - dt.timedelta(days=round(MONITOR_MONTHS * 30.44))).date()
    pb = s.query(ProgramBroker.broker_party_id, ProgramBroker.program_id).join(
        Program, Program.id == ProgramBroker.program_id)
    if r.tenants is not None:
        pb = pb.filter(Program.tenant_id.in_(sorted(r.tenants) or [0]))
    if broker:
        pb = pb.filter(ProgramBroker.broker_party_id == broker)
    if programme:
        pb = pb.filter(ProgramBroker.program_id == programme)
    progs: dict[int, set] = {}
    for b, p in pb.all():
        if b:
            progs.setdefault(b, set()).add(p)
    if not progs:
        return []
    eq = r.filter(s.query(E), E.tenant_id, E.broker_party_id).filter(
        E.broker_party_id.in_(list(progs)), E.due_date >= since, E.due_date <= today)
    if programme:
        eq = eq.filter(E.program_id == programme)
    late: dict[int, int] = {}
    missing: dict[int, int] = {}
    for e in eq:
        st = _state(e, today)
        if st == "received_late":
            late[e.broker_party_id] = late.get(e.broker_party_id, 0) + 1
        elif st in ("overdue", "never_arrived"):
            missing[e.broker_party_id] = missing.get(e.broker_party_id, 0) + 1
    fq = r.filter(s.query(FileArrival.matched_broker_party_id, func.count()), FileArrival.tenant_id,
                  FileArrival.matched_broker_party_id).filter(
        FileArrival.matched_broker_party_id.in_(list(progs)),
        FileArrival.received_at >= dt.datetime.combine(since, dt.time()), FileArrival.outcome == "accepted")
    if programme:
        fq = fq.filter(FileArrival.program_id == programme)
    sent = dict(fq.group_by(FileArrival.matched_broker_party_id).all())
    tenant = next(iter(r.tenants)) if r.tenants and len(r.tenants) == 1 else None
    findings = am.open_findings_by_broker(s, tenant)
    names = _names(s, (), list(progs))
    from db import Party
    kinds = dict(s.query(Party.id, Party.party_type).filter(Party.id.in_(list(progs))))
    rows = []
    for b, ps in progs.items():
        f = findings.get(b, {"open": 0, "critical": 0})
        level, why = risk_of(late.get(b, 0), missing.get(b, 0), f["open"], f["critical"])
        kind = str(getattr(kinds.get(b), "value", kinds.get(b)) or "broker")
        rows.append({"broker_id": b, "broker": names["party"].get(b) or f"#{b}",
                     "type": kind, "type_label": PARTNER_TYPES.get(kind, kind.replace("_", " ").capitalize()),
                     "programmes": len(ps),
                     "files_sent": int(sent.get(b, 0)), "late": late.get(b, 0), "missing": missing.get(b, 0),
                     "open_findings": f["open"], "critical_findings": f["critical"],
                     "risk": level, "reasons": why})
    order = {"High": 0, "Medium": 1, "Low": 2}
    rows.sort(key=lambda x: (order[x["risk"]], -x["late"] - x["missing"], x["broker"].lower()))
    return rows


def partners(s, principal, *, mga=None, broker=None, programme=None, risk=None, kind=None,
             page: int = 1, page_size: int = 50, today=None) -> dict:
    """Every broker of the carrier: programmes, files sent, late, missing, open
    findings and a risk level. Carrier and Kavachio only — a broker never sees
    how other brokers are doing."""
    import audit_management as am
    r = reach(s, principal, mga)
    if r.v.is_broker:
        raise HTTPException(403, "Partner Monitoring is for carriers.")
    rows = _monitor_rows(s, r, broker=broker, programme=programme, today=today)
    shown = [x for x in rows if (not risk or x["risk"] == risk) and (not kind or x["type"] == kind)]
    out = _page(shown, page, page_size)
    out["counts"] = {"brokers": len(rows), "high": sum(1 for x in rows if x["risk"] == "High"),
                     "open_findings": sum(x["open_findings"] for x in rows),
                     "late": sum(x["late"] for x in rows)}
    out["types"] = [{"id": k, "name": v} for k, v in sorted({x["type"]: x["type_label"] for x in rows}.items(),
                                                           key=lambda kv: kv[1])]
    out["rule"] = RISK_RULE
    out["months"] = MONITOR_MONTHS
    out["audits_ready"] = not am.missing_tables(s)
    return out


def partner_profile(s, principal, broker_id: int, mga=None, today=None) -> dict:
    import audit_management as am
    from db import Program, ProgramBroker
    r = reach(s, principal, mga)
    if r.v.is_broker:
        raise HTTPException(403, "Partner Monitoring is for carriers.")
    rows = _monitor_rows(s, r, broker=broker_id, today=today)
    if not rows:
        raise HTTPException(404, "That broker is not one of yours.")
    row = rows[0]
    pq = s.query(Program.id, Program.name).join(ProgramBroker, ProgramBroker.program_id == Program.id).filter(
        ProgramBroker.broker_party_id == broker_id)
    if r.tenants is not None:
        pq = pq.filter(Program.tenant_id.in_(sorted(r.tenants) or [0]))
    row["programme_list"] = [{"id": i, "name": n} for i, n in sorted(set(pq.all()), key=lambda x: (x[1] or "").lower())]
    row["findings"] = []
    if not am.missing_tables(s):
        q = s.query(am.Finding).filter(am.Finding.broker_party_id == broker_id, am.Finding.status != "closed")
        if r.tenants is not None:
            q = q.filter(am.Finding.tenant_id.in_(sorted(r.tenants) or [0]))
        row["findings"] = [{"id": f.id, "audit_id": f.audit_id, "title": f.title,
                            "severity": am.SEVERITIES.get(f.severity, f.severity),
                            "status": am.FINDING_STATUS.get(f.status, f.status), "due_date": _iso(f.due_date),
                            "overdue": am.overdue(f.due_date, f.status)} for f in q.order_by(am.Finding.id.desc())]
    row["rule"] = RISK_RULE
    return row


# ── Setups made active again (51.1) ────────────────────────────────────────
def restores(s, principal, *, mga=None, page: int = 1, page_size: int = 50) -> dict:
    """Every earlier Bordereau Setup made active again, newest first, with the
    reason and what it replaced. A carrier's own decision: brokers see none."""
    from db import ActivityEvent
    import setup_restore as sr
    r = reach(s, principal, mga)
    if r.v.is_broker:
        return {**_page([], page, page_size)}
    q = s.query(ActivityEvent).filter(ActivityEvent.action == sr.ACTION)
    if r.tenants is not None:
        q = q.filter(ActivityEvent.tenant_id.in_(sorted(r.tenants) or [0]))
    people = People(s, r.v)
    items = []
    for e in q.order_by(ActivityEvent.created_at.desc()).limit(2000):
        d = e.details if isinstance(e.details, dict) else {}
        items.append({"at": _iso(e.created_at), "setup": d.get("name"), "setup_id": d.get("pipeline_id"),
                      "programme": d.get("programme") or "—", "broker": d.get("broker") or "—",
                      "replaced": ", ".join(x.get("name") or "" for x in d.get("replaced") or []) or "Nothing",
                      "reason": d.get("reason"), "by": people.name(e.actor_user_id, e.actor or "—")})
    return _page(items, page, page_size)
