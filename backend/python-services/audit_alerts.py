"""
audit_alerts.py
───────────────
Who hears about an audit finding (53.2), so a finding is followed to closure
without anyone having to go and look.

  EMAILS (notifications.notify_people, the app's own sender and layout)
    a finding raised          → the broker's contacts; the carrier who raised it is copied
    the broker replied        → the carrier
    the carrier replied       → the broker's contacts
    due in REMIND_DAYS days   → the broker's contacts, once
    overdue                   → the broker's contacts, the carrier copied, once

  REMINDERS run in the daily sweep (sweep_scheduler). Each one sent is an
  activity_events row "audit_finding_reminder" naming the finding and which
  reminder — that row is what stops a second send, and it is the Audit Trail's
  "Reminder sent" line. Closed findings get none.

  DASHBOARD CARDS read waiting_for_carrier() / the broker's own findings.

Telling never undoes the action it reports: every sender here swallows its errors.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

import audit_management as am

log = logging.getLogger("audit_alerts")

REMIND_DAYS = 3
SUBJECT_PREFIX = "Kavachio - "
BROKER_LINK = "/broker/audit-findings"
BROKER_LINK_LABEL = "Open your Audit Findings"
BROKER_FOOTER = "You're receiving this because you're a contact for this broker on Kavachio."
CARRIER_FOOTER = "You're receiving this because you're the carrier for this Kavachio account."
REMINDER_ACTION = "audit_finding_reminder"
REMINDER_WORDS = {"due_soon": "due soon", "overdue": "overdue"}
SHORT = 300


def _day(d: Optional[dt.date]) -> str:
    return f"{d:%d %b %Y}" if d else "Not set"


def _short(text: Optional[str]) -> str:
    t = (text or "").strip()
    return t if len(t) <= SHORT else t[:SHORT - 1].rstrip() + "…"


def carrier_link(a: am.Audit) -> str:
    return f"/audit?tab=audits&audit={a.id}"


def _carrier_name(s, tenant_id: int) -> str:
    from db import Tenant
    t = s.get(Tenant, tenant_id)
    return (t.legal_name or t.tenant_name) if t else "Your carrier"


def _broker_name(s, broker_party_id: int) -> str:
    from db import Party
    p = s.get(Party, broker_party_id)
    return p.legal_name if p and p.legal_name else "The broker"


def _broker_people(s, broker_party_id: int) -> list[dict]:
    from submission_calendar_service import broker_contacts
    return [{"email": c["email"], "name": c.get("name")}
            for c in broker_contacts(s, [broker_party_id]).get(broker_party_id, []) if c.get("email")]


def _email_of(s, user_id: Optional[int]) -> Optional[str]:
    from db import AppUser
    u = s.get(AppUser, user_id) if user_id else None
    return (u.email or "").strip() or None if u else None


def _facts(s, a: am.Audit, f: am.Finding) -> list[tuple[str, str]]:
    from db import Contract
    out = [("Audit", f"{a.name} ({am.audit_ref(a)})"), ("Finding", f.title),
           ("Severity", am.SEVERITIES.get(f.severity, f.severity)), ("Due by", _day(f.due_date))]
    c = s.get(Contract, a.contract_id) if a.contract_id else None
    if c is not None:
        out.append(("Contract", c.name or c.filename or f"Contract #{c.id}"))
    return out


def _send(people, subject, title, body, facts, link, label, footer, cc=None, reply_to=None) -> bool:
    if not people:
        return False
    from notifications import notify_people
    notify_people(people, title, body=body, facts=facts, link_path=link, link_label=label,
                  subject=SUBJECT_PREFIX + subject, footer=footer, cc=cc, reply_to=reply_to)
    return True


# ── when something happens ─────────────────────────────────────────────────
def finding_raised(s, a: am.Audit, f: am.Finding, by_user_id: Optional[int]) -> None:
    try:
        me = _email_of(s, by_user_id)
        carrier = _carrier_name(s, a.tenant_id)
        _send(_broker_people(s, f.broker_party_id), f"New audit finding: {f.title}",
              f"{carrier} raised an audit finding for you",
              "Please reply with what you have done to fix it and attach any evidence.",
              _facts(s, a, f), BROKER_LINK, BROKER_LINK_LABEL, BROKER_FOOTER,
              cc=[me] if me else None, reply_to=me)
    except Exception as e:  # noqa: BLE001
        log.warning("could not email the broker about finding %s: %s", f.id, e)


def broker_replied(s, a: am.Audit, f: am.Finding, note: Optional[str], file_name: Optional[str]) -> None:
    try:
        from notifications import carrier_admin_recipients
        broker = _broker_name(s, f.broker_party_id)
        facts = [("Audit", f"{a.name} ({am.audit_ref(a)})"), ("Finding", f.title),
                 ("Reply", _short(note) or "No note"), ("Evidence", file_name or "None")]
        _send(carrier_admin_recipients(a.tenant_id), f"{broker} replied to an audit finding",
              f"{broker} replied to “{f.title}”",
              "Check their reply and evidence, then close the finding or ask for more.",
              facts, carrier_link(a), "Open the audit", CARRIER_FOOTER)
    except Exception as e:  # noqa: BLE001
        log.warning("could not email the carrier about a reply on finding %s: %s", f.id, e)


def carrier_replied(s, a: am.Audit, f: am.Finding, note: Optional[str], by_user_id: Optional[int]) -> None:
    try:
        me = _email_of(s, by_user_id)
        carrier = _carrier_name(s, a.tenant_id)
        facts = [("Audit", f"{a.name} ({am.audit_ref(a)})"), ("Finding", f.title),
                 ("Reply", _short(note) or "No note"), ("Due by", _day(f.due_date))]
        _send(_broker_people(s, f.broker_party_id), f"{carrier} replied about an audit finding",
              f"{carrier} replied about “{f.title}”", "Read the reply and send what is asked.",
              facts, BROKER_LINK, BROKER_LINK_LABEL, BROKER_FOOTER, reply_to=me)
    except Exception as e:  # noqa: BLE001
        log.warning("could not email the broker about a reply on finding %s: %s", f.id, e)


# ── reminders (the daily sweep) ────────────────────────────────────────────
def reminder_due(f: am.Finding, today: dt.date) -> Optional[str]:
    """Which reminder a finding is owed today, if any — before sent ones are ruled out."""
    if f.status == "closed" or f.due_date is None:
        return None
    if f.due_date < today:
        return "overdue"
    if (f.due_date - today).days <= REMIND_DAYS:
        return "due_soon"
    return None


def _sent(s, finding_ids) -> set[tuple[int, str]]:
    from db import ActivityEvent
    if not finding_ids:
        return set()
    targets = [f"audit_finding:{i}" for i in finding_ids]
    out = set()
    for target, details in (s.query(ActivityEvent.target, ActivityEvent.details)
                            .filter(ActivityEvent.action == REMINDER_ACTION,
                                    ActivityEvent.target.in_(targets))):
        said = (details or {}).get("reminder") if isinstance(details, dict) else None
        kind = {w: k for k, w in REMINDER_WORDS.items()}.get(said)
        if kind:
            out.add((int(target.split(":")[1]), kind))
    return out


def send_reminders(s, today: Optional[dt.date] = None) -> int:
    """Every reminder owed today, once each. Returns how many went out."""
    from db import ActivityEvent
    if am.missing_tables(s):
        return 0
    today = today or am._today()
    fs = (s.query(am.Finding).filter(am.Finding.status != "closed", am.Finding.due_date.isnot(None),
                                     am.Finding.due_date <= today + dt.timedelta(days=REMIND_DAYS)).all())
    done = _sent(s, [f.id for f in fs])
    sent = 0
    for f in fs:
        kind = reminder_due(f, today)
        # A finding already overdue when first seen gets only the overdue one.
        if kind is None or (f.id, kind) in done:
            continue
        a = s.get(am.Audit, f.audit_id)
        if a is None:
            continue
        try:
            people = _broker_people(s, f.broker_party_id)
            if kind == "due_soon":
                n = (f.due_date - today).days
                ok = _send(people, f"Audit finding due in {n} day{'' if n == 1 else 's'}: {f.title}"
                           if n > 0 else f"Audit finding due today: {f.title}",
                           f"An audit finding is due on {_day(f.due_date)}",
                           "Reply with what you have done to fix it before the due date.",
                           _facts(s, a, f), BROKER_LINK, BROKER_LINK_LABEL, BROKER_FOOTER)
            else:
                from notifications import carrier_admin_recipients
                cc = [p["email"] for p in carrier_admin_recipients(a.tenant_id) if p.get("email")]
                ok = _send(people, f"Audit finding overdue: {f.title}", "An audit finding is overdue",
                           f"This finding was due on {_day(f.due_date)} and is still open. "
                           "Please reply as soon as you can.",
                           _facts(s, a, f), BROKER_LINK, BROKER_LINK_LABEL, BROKER_FOOTER, cc=cc or None)
        except Exception as e:  # noqa: BLE001
            log.warning("audit reminder for finding %s failed: %s", f.id, e)
            continue
        if not ok:
            continue          # nobody to tell — try again tomorrow, once a contact exists
        s.add(ActivityEvent(tenant_id=a.tenant_id, actor="Kavachio · Automatic", action=REMINDER_ACTION,
                            target=f"audit_finding:{f.id}",
                            details={"audit_id": a.id, "audit": am.audit_ref(a), "name": a.name,
                                     "finding_id": f.id, "finding": f.title[:120], "reminder": REMINDER_WORDS[kind],
                                     "due_date": f.due_date.isoformat()}))
        sent += 1
    s.commit()
    return sent


# ── the carrier's dashboard card ───────────────────────────────────────────
def waiting_for_carrier(s, tenant_id: Optional[int], today: Optional[dt.date] = None) -> dict:
    """Findings the broker answered last (the carrier's turn) and findings past due."""
    if am.missing_tables(s):
        return {"ready": False, "replied": [], "overdue": []}
    today = today or am._today()
    q = s.query(am.Finding).filter(am.Finding.status != "closed")
    fs = (q.filter(am.Finding.tenant_id == tenant_id) if tenant_id is not None else q).all()
    events = am._events(s, [f.id for f in fs])
    audits = {a.id: a for a in s.query(am.Audit).filter(am.Audit.id.in_({f.audit_id for f in fs} or {0}))}
    names = am._names(s, {f.broker_party_id for f in fs})
    replied, late = [], []
    for f in fs:
        a = audits.get(f.audit_id)
        people = [e for e in events.get(f.id, []) if e.by_user_id is not None]
        row = {"id": f.id, "audit_id": f.audit_id, "audit": a.name if a else None,
               "audit_ref": am.audit_ref(a) if a else None, "title": f.title,
               "severity": f.severity, "severity_label": am.SEVERITIES.get(f.severity, f.severity),
               "broker": names["party"].get(f.broker_party_id), "due_date": am._iso(f.due_date)}
        if people and people[-1].by_side == "broker":
            replied.append({**row, "replied_at": am._iso(people[-1].at)})
        if am.overdue(f.due_date, f.status, today):
            late.append(row)
    replied.sort(key=lambda r: r["replied_at"] or "")
    late.sort(key=lambda r: r["due_date"] or "")
    return {"ready": True, "replied": replied, "overdue": late}
