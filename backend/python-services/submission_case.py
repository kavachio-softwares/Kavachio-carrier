"""MOPR 39.2 — what is being done about a missing bordereau.

submission_calendar.derive_status (39.1) answers "did the file arrive?". This
module answers the next question a carrier asks about one that did not: "what
are we doing about it?" — a CASE on the period, in five stages:

  overdue     opened automatically the day after the due date, nothing in
  delinquent  automatically, once it is `delinquent_after_days` days late
              (default 15, set per programme on its schedule)
  in_dispute  the broker says the mark is wrong — or the carrier enters it for
              them. No automatic move, no reminder and no chase happens until
              the carrier decides
  extended    the carrier agreed more time; waited for until `case_until`,
              after which it is overdue again, counted from that date
  resolved    closed: the file arrived (automatically), or the carrier accepted
              the dispute or waived the period (nil return)

Two answers, kept apart on purpose. A file that came 32 days late is
received_late in 39.1 AND resolved/file_received here; a waived period is still
never_arrived in 39.1 (no file ever came) but resolved/waived here.

Who may do what:
  * the BROKER raises a dispute, on a file that is Overdue or Delinquent, once —
    after the carrier rejects one only the carrier can reopen it, or disputes
    could pause the chase for ever;
  * the CARRIER ADMIN accepts or rejects a dispute, waives the period, gives
    more time, or enters a dispute on the broker's behalf.

Every move is written to submission_case_event (who, the day it counts from,
why) and to the activity feed (the bell and the Audit Logs). The state lives on
the period; the history is what a dispute is settled against and what the MOPR
pack counts.

Callers commit. Nothing here sends mail inside a decision — the routes call
notify_* after the commit, so a dead mailbox never undoes one.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable, Optional

from sqlalchemy import and_, or_

from db import ActivityEvent, ExpectedSubmission, SubmissionCaseEvent, SubmissionSchedule

OVERDUE, DELINQUENT, IN_DISPUTE, EXTENDED, RESOLVED = (
    "overdue", "delinquent", "in_dispute", "extended", "resolved")
# A case still going: the file is owed and something is happening about it.
OPEN = (OVERDUE, DELINQUENT, IN_DISPUTE, EXTENDED)
# Where a dispute can be raised from: late, and nobody has decided anything.
DISPUTABLE = (OVERDUE, DELINQUENT)
# Nobody is chased about these: a dispute waits on the carrier, more time was
# agreed, or the case is closed.
NO_CHASE = (IN_DISPUTE, EXTENDED, RESOLVED)

# How a case closed.
FILE_RECEIVED, WAIVED, ACCEPTED = "file_received", "waived", "accepted"

STATE_WORDS = {
    OVERDUE: "Overdue", DELINQUENT: "Delinquent", IN_DISPUTE: "In Dispute",
    EXTENDED: "More time given", RESOLVED: "Resolved",
}
RESOLUTION_WORDS = {
    FILE_RECEIVED: "File received",
    WAIVED: "No file needed",
    ACCEPTED: "Dispute accepted",
}
DISPUTE_REASONS = {
    "nil_return": "No policies sold this period, so nothing to report",
    "already_sent": "Already sent by email or another way",
    "deadline_wrong": "The due date is wrong",
    "extension_agreed": "The carrier already agreed a later date",
    "other": "Other",
}
EVENT_WORDS = {
    "opened": "Became overdue",
    "delinquent": "Became delinquent",
    "dispute_raised": "Dispute raised",
    "dispute_accepted": "Dispute accepted",
    "dispute_rejected": "Dispute rejected",
    "waived": "Marked as no file needed",
    "extended": "More time given",
    "reopened": "Extra time ran out",
    "case_reopened": "Case reopened by carrier",
    "file_received": "File received — case closed",
    "cleared": "No longer late — the due date moved",
    "carrier_reminded": "Carrier reminded to decide",
}
DECISIONS = ("accept", "reject", "waive", "extend", "reopen")
# A closed case the carrier can take back: one IT closed. A case closed by the
# file arriving is not reopened — a wrong file is corrected by sending a new
# version, not by pretending none came.
REOPENABLE = (ACCEPTED, WAIVED)

DEFAULT_DELINQUENT_AFTER_DAYS = 15
# A dispute the carrier has not decided after this many days earns them ONE
# reminder. It stays open — nothing is ever decided for them.
DISPUTE_REMIND_AFTER_DAYS = 5
# A move the daily check finds it should have made more than this many days
# ago is backlog (the check had not run, or this feature has just been switched
# on). It is still recorded, with the day it really counts from, but rings no
# bell: fifty "delinquent" bells about last spring would bury today's.
FRESH_DAYS = 3

SYSTEM = {"actor": "system"}


class CaseError(ValueError):
    """A move that is not allowed. `code` is what a route turns into a status."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def delinquent_after_days(schedule) -> int:
    """Days late before Delinquent, for one programme's schedule."""
    n = getattr(schedule, "delinquent_after_days", None) if schedule is not None else None
    try:
        n = int(n)
    except (TypeError, ValueError):
        return DEFAULT_DELINQUENT_AFTER_DAYS
    return n if n >= 1 else DEFAULT_DELINQUENT_AFTER_DAYS


def deadline_of(e) -> Optional[date]:
    """The day the file is late FROM: an agreed new date when more time was
    given, the due date otherwise."""
    return e.case_until or e.due_date


def effective_state(e, today: date) -> Optional[str]:
    """The case as it stands, including one the daily check has not opened yet:
    a late file with no case reads as Overdue, which is what the check will
    make it."""
    if e.case_state:
        return e.case_state
    if (e.received_at is None and e.broker_party_id is not None
            and e.due_date is not None and today > e.due_date):
        return OVERDUE
    return None


def days_over(e, today: date) -> Optional[int]:
    """Days past the due date with nothing in, for the screens.

    Counted to today while the file is still owed, but STOPPED on the day the
    carrier said nothing more is owed (dispute accepted, or waived): a closed
    case that kept counting read as a broker who was still late. The stopped
    figure is how late it was when it was settled. None once a file arrived
    (that is days LATE, not days over) or before the due date."""
    if e.received_at is not None or e.due_date is None:
        return None
    end = today
    if (e.case_state == RESOLVED and e.case_resolution in (ACCEPTED, WAIVED)
            and e.case_since is not None):
        end = min(today, e.case_since)
    return (end - e.due_date).days if end > e.due_date else None


def _iso(d) -> Optional[str]:
    return d.isoformat() if d else None


def _days(n: int) -> str:
    return f"{n} day{'' if n == 1 else 's'}"


def _record(session, e, kind: str, *, from_state, to_state, on: date,
            who: Optional[dict] = None, reason: Optional[str] = None,
            note: Optional[str] = None, details: Optional[dict] = None,
            activity: Optional[str] = None) -> None:
    """Append one history row — and, when `activity` is named, one activity
    event (the bell and the Audit Logs read those)."""
    who = who or SYSTEM
    session.add(SubmissionCaseEvent(
        tenant_id=e.tenant_id, expected_id=e.id, program_id=e.program_id,
        broker_party_id=e.broker_party_id, period=e.period, kind=kind,
        from_state=from_state, to_state=to_state, effective_on=on,
        reason=reason, note=note, details=details, actor=who.get("actor"),
        actor_user_id=who.get("user_id"), actor_role=who.get("role"),
        actor_broker_party_id=who.get("broker_party_id")))
    if activity:
        info = {"program_id": e.program_id, "period": e.period,
                "expected_id": e.id, "broker_party_id": e.broker_party_id,
                "due_date": _iso(e.due_date), "state": to_state,
                "effective_on": _iso(on)}
        if reason:
            info["reason"] = reason
        if note:
            info["note"] = note
        info.update(details or {})
        session.add(ActivityEvent(
            tenant_id=e.tenant_id, actor=who.get("actor") or "system",
            action=activity, target=f"program:{e.program_id}", details=info,
            actor_user_id=who.get("user_id"), actor_role=who.get("role"),
            actor_broker_party_id=who.get("broker_party_id")))


def _move(session, e, to_state: Optional[str], kind: str, *, on: date, **kw) -> None:
    frm = e.case_state
    e.case_state = to_state
    e.case_since = on if to_state else None
    _record(session, e, kind, from_state=frm, to_state=to_state, on=on, **kw)


# --- the automatic half ------------------------------------------------------

def advance(session, e, today: date, n: int, *, ring: bool = True) -> list[str]:
    """Make every automatic move this period is due as of `today`, in order,
    each recorded with the day it really counts from. Returns what was done.

    Nothing happens to a file that has arrived, a period no broker owes, a case
    in dispute (apart from the one reminder to the carrier) or a closed case.
    """
    made: list[str] = []
    if e.received_at is not None or e.broker_party_id is None or e.due_date is None:
        return made
    st = e.case_state

    if st is None:
        if today <= e.due_date:
            return made
        _move(session, e, OVERDUE, "opened", on=e.due_date + timedelta(days=1),
              note=f"Due {e.due_date:%d %b %Y}; nothing received")
        made.append("opened")
        st = OVERDUE

    if st == EXTENDED:
        if e.case_until is None or today <= e.case_until:
            return made
        on = e.case_until + timedelta(days=1)
        _move(session, e, OVERDUE, "reopened", on=on,
              note=f"The extra time ran out on {e.case_until:%d %b %Y}",
              activity=("submission_case_reopened"
                        if ring and (today - on).days <= FRESH_DAYS else None))
        made.append("reopened")
        st = OVERDUE

    if st in (OVERDUE, DELINQUENT):
        deadline = deadline_of(e)
        if today <= deadline:
            # The schedule was changed and the due date moved past today: the
            # file is not late any more, so there is nothing to chase.
            e.case_until = None
            _move(session, e, None, "cleared", on=today,
                  note=f"The due date is now {deadline:%d %b %Y}")
            made.append("cleared")
            return made
        if st == OVERDUE:
            on = deadline + timedelta(days=n)
            if today >= on:
                _move(session, e, DELINQUENT, "delinquent", on=on,
                      note=f"{_days(n)} late with nothing received",
                      activity=("submission_delinquent"
                                if ring and (today - on).days <= FRESH_DAYS else None))
                made.append("delinquent")
        return made

    if st == IN_DISPUTE:
        raised = e.dispute_raised_on
        if (not e.dispute_reminded and raised is not None
                and today >= raised + timedelta(days=DISPUTE_REMIND_AFTER_DAYS)):
            e.dispute_reminded = True
            _record(session, e, "carrier_reminded", from_state=IN_DISPUTE,
                    to_state=IN_DISPUTE, on=today,
                    note=f"Waiting {_days((today - raised).days)} for a decision",
                    activity="submission_dispute_waiting" if ring else None)
            made.append("carrier_reminded")
    return made


def sweep_cases(session, today: Optional[date] = None,
                tenant_id: Optional[int] = None) -> dict:
    """The daily check: every automatic move every late file is due. Safe to
    run any number of times — a move already made is never made twice.
    Caller commits."""
    today = today or datetime.utcnow().date()
    sched_q = session.query(SubmissionSchedule)
    # Fresh rows, never a stale copy this session loaded earlier: a move is
    # decided from case_state, and an out-of-date one makes it twice. Flushed
    # first because sessions here do not autoflush.
    session.flush()
    q = session.query(ExpectedSubmission).populate_existing().filter(
        ExpectedSubmission.received_at.is_(None),
        ExpectedSubmission.broker_party_id.isnot(None),
        or_(and_(ExpectedSubmission.case_state.is_(None),
                 ExpectedSubmission.due_date < today),
            ExpectedSubmission.case_state.in_(OPEN)))
    if tenant_id is not None:
        sched_q = sched_q.filter(SubmissionSchedule.tenant_id == tenant_id)
        q = q.filter(ExpectedSubmission.tenant_id == tenant_id)
    schedules = {s.program_id: s for s in sched_q.all()}
    counts: dict[str, int] = {}
    for e in q.all():
        for kind in advance(session, e, today,
                            delinquent_after_days(schedules.get(e.program_id))):
            counts[kind] = counts.get(kind, 0) + 1
    session.flush()
    return counts


def on_file_received(session, e, received_on: date) -> bool:
    """The file arrived: any case still going closes by itself. Called once,
    for the period's first file — a correction changes nothing here."""
    if e.case_state not in OPEN:
        return False
    e.case_resolution = FILE_RECEIVED
    _move(session, e, RESOLVED, "file_received", on=received_on,
          reason=FILE_RECEIVED, note=f"File received on {received_on:%d %b %Y}",
          activity="submission_case_resolved")
    return True


# --- the human half ----------------------------------------------------------

def _clean(text: Optional[str]) -> Optional[str]:
    text = (text or "").strip()
    return text or None


def _assert_owed(e) -> None:
    if e.received_at is not None:
        raise CaseError("received",
                        "The file has already arrived, so this case closed by itself.")
    if e.broker_party_id is None:
        raise CaseError("no_broker", "No broker owes this period, so there is no case.")


def raise_dispute(session, e, *, who: dict, reason: str, note: Optional[str],
                  by_carrier: bool = False, today: Optional[date] = None,
                  n: int = DEFAULT_DELINQUENT_AFTER_DAYS) -> dict:
    """The broker says the mark is wrong — or the carrier enters that for them
    (`by_carrier`). Pauses everything automatic until the carrier decides."""
    today = today or datetime.utcnow().date()
    _assert_owed(e)
    advance(session, e, today, n, ring=False)   # open it if the check has not
    if e.case_state not in DISPUTABLE:
        raise CaseError(
            "not_disputable",
            "A dispute can be raised only on a late file that is Overdue or "
            f"Delinquent — this one is {STATE_WORDS.get(e.case_state, 'not late')}.")
    if e.dispute_locked and not by_carrier:
        raise CaseError(
            "locked", "The carrier has already rejected a dispute on this file. "
                      "Only the carrier can reopen it.")
    reason = (reason or "").strip()
    if reason not in DISPUTE_REASONS:
        raise CaseError("bad_reason", "Pick a reason for the dispute.")
    note = _clean(note)
    if reason == "other" and not note:
        raise CaseError("note_required", "Say what the dispute is about.")
    e.dispute_reason = reason
    e.dispute_note = note
    e.dispute_raised_on = today
    e.dispute_by_carrier = bool(by_carrier)
    e.dispute_reminded = False
    _move(session, e, IN_DISPUTE, "dispute_raised", on=today, who=who,
          reason=reason, note=note,
          details={"entered_by_carrier": True} if by_carrier else None,
          activity=("submission_dispute_entered" if by_carrier
                    else "submission_dispute_raised"))
    session.flush()
    return case_dict(e)


def decide(session, e, *, who: dict, decision: str, note: Optional[str],
           extend_to: Optional[date] = None, today: Optional[date] = None,
           n: int = DEFAULT_DELINQUENT_AFTER_DAYS) -> dict:
    """The carrier's decision on a case:

      accept  the dispute is right            → Resolved (accepted)
      reject  it is not                       → back to Delinquent or Overdue,
                                                 and the broker cannot dispute again
      waive   nothing is owed (nil return)    → Resolved (waived)
      extend  more time, until `extend_to`    → More time given
      reopen  an accept or waive was wrong    → back to Delinquent or Overdue,
                                                 and the broker cannot dispute again

    A note is required every time: the broker is told it.
    """
    today = today or datetime.utcnow().date()
    if decision not in DECISIONS:
        raise CaseError("bad_decision",
                        "Choose accept, reject, no file needed, more time or reopen.")
    note = _clean(note)
    if not note:
        raise CaseError("note_required", "Add a note saying why — the broker is told it.")
    _assert_owed(e)
    advance(session, e, today, n, ring=False)
    st = e.case_state
    if decision in ("accept", "reject") and st != IN_DISPUTE:
        raise CaseError("no_dispute",
                        "There is no open dispute on this file to accept or reject.")
    if decision in ("waive", "extend") and st not in OPEN:
        raise CaseError("not_open", "This file has no open case.")
    if decision == "reopen" and not (st == RESOLVED
                                     and e.case_resolution in REOPENABLE):
        raise CaseError("not_reopenable",
                        "Only a case closed by an accepted dispute or 'No file "
                        "needed' can be reopened.")

    if decision == "accept":
        e.case_resolution = ACCEPTED
        _move(session, e, RESOLVED, "dispute_accepted", on=today, who=who,
              reason=ACCEPTED, note=note, activity="submission_dispute_accepted")
    elif decision == "reject":
        e.dispute_locked = True
        back = DELINQUENT if (today - deadline_of(e)).days >= n else OVERDUE
        _move(session, e, back, "dispute_rejected", on=today, who=who, note=note,
              activity="submission_dispute_rejected")
    elif decision == "waive":
        e.case_resolution = WAIVED
        _move(session, e, RESOLVED, "waived", on=today, who=who, reason=WAIVED,
              note=note, activity="submission_waived")
    elif decision == "reopen":
        # The file is owed again, judged from its ORIGINAL due date — any more
        # time agreed before the case closed went with it. Like a rejection,
        # the broker cannot dispute it again; the carrier still can for it.
        was = e.case_resolution
        e.case_resolution = None
        e.case_until = None
        e.dispute_locked = True
        back = DELINQUENT if (today - deadline_of(e)).days >= n else OVERDUE
        # Its own activity name: "submission_case_reopened" is the automatic
        # "extra time ran out", which rings the carrier's bell.
        _move(session, e, back, "case_reopened", on=today, who=who, note=note,
              details={"was": was}, activity="submission_case_reopened_by_carrier")
    else:
        if extend_to is None or extend_to <= today:
            raise CaseError("bad_date", "Pick a new date after today.")
        e.case_until = extend_to
        _move(session, e, EXTENDED, "extended", on=today, who=who, note=note,
              details={"until": extend_to.isoformat()}, activity="submission_extended")
    session.flush()
    return case_dict(e)


# --- reading it back ---------------------------------------------------------

def case_dict(e) -> dict:
    """Where the case stands — what the calendars show on the row."""
    st = e.case_state
    res = e.case_resolution if st == RESOLVED else None
    return {
        "state": st,
        "state_label": STATE_WORDS.get(st) if st else None,
        "since": _iso(e.case_since),
        "until": _iso(e.case_until) if st in (EXTENDED, OVERDUE, DELINQUENT) else None,
        "resolution": res,
        "resolution_label": RESOLUTION_WORDS.get(res) if res else None,
        "dispute_reason": e.dispute_reason,
        "dispute_reason_label": DISPUTE_REASONS.get(e.dispute_reason or ""),
        "dispute_note": e.dispute_note,
        "dispute_raised_on": _iso(e.dispute_raised_on),
        "dispute_by_carrier": bool(e.dispute_by_carrier),
        "dispute_locked": bool(e.dispute_locked),
    }


def allowed(e, *, side: str, carrier_admin: bool, today: Optional[date] = None) -> dict:
    """What THIS viewer may do on the case now — the buttons a screen shows.
    The routes enforce the same rules; this only saves a screen guessing."""
    today = today or datetime.utcnow().date()
    st = effective_state(e, today)
    owed = e.received_at is None and e.broker_party_id is not None
    if side == "broker":
        return {"raise_dispute": owed and st in DISPUTABLE and not e.dispute_locked}
    if not carrier_admin:
        return {}
    return {
        "raise_dispute": owed and st in DISPUTABLE,
        "accept": owed and st == IN_DISPUTE,
        "reject": owed and st == IN_DISPUTE,
        "waive": owed and st in OPEN,
        "extend": owed and st in OPEN,
        "reopen": owed and st == RESOLVED and e.case_resolution in REOPENABLE,
    }


def history(session, expected_id: int,
            label: Optional[Callable[[int], Optional[str]]] = None) -> list[dict]:
    """Every move on one period's case, oldest first. `label` turns a user id
    into the words this viewer may see (decision_log.Labeller.label): a broker
    is shown the carrier, never a carrier person's email, and vice versa."""
    from auth_deps import BROKER_ROLES
    rows = (session.query(SubmissionCaseEvent)
            .filter(SubmissionCaseEvent.expected_id == expected_id)
            .order_by(SubmissionCaseEvent.id.asc()).all())
    out = []
    for r in rows:
        if (r.actor or "") == "system" or r.actor_user_id is None:
            by = "Automatic"
        else:
            by = (label(r.actor_user_id) if label else None) or (
                "The broker" if r.actor_role in BROKER_ROLES else "The carrier")
        out.append({
            "id": r.id,
            "kind": r.kind,
            "kind_label": EVENT_WORDS.get(r.kind, r.kind),
            "from_state": r.from_state,
            "to_state": r.to_state,
            "to_label": STATE_WORDS.get(r.to_state) if r.to_state else "No case",
            "on": _iso(r.effective_on),
            "at": (r.created_at.isoformat() + "Z") if r.created_at else None,
            "reason": r.reason,
            # Only a dispute's reason adds anything. How a case closed
            # (accepted / waived / file received) is already the event's own
            # name, and printing it under that name read as a repeat.
            "reason_label": DISPUTE_REASONS.get(r.reason or ""),
            "note": r.note,
            "details": r.details or {},
            "by": by,
        })
    return out


# --- telling people (routes call these AFTER the commit) ---------------------

def notify_dispute_raised(session, e, program_name: str, broker_name: str,
                          raised_by: Optional[str] = None) -> None:
    """Mail the carrier admin that a broker has disputed a late file. Never
    raises — the dispute is already saved.

    The broker's admins are copied, so the broker holds what the carrier was
    told, and a reply goes to whoever raised it. Only ADMINS, never the
    broker's own users: the carrier is never shown those (broker_contacts,
    audit.actor_for), so one who raised it is answered through the admin."""
    try:
        from notifications import (CARRIER_ADMIN_FOOTER, carrier_admin_recipients,
                                   notify_people)
        from submission_calendar_service import broker_contacts
        admins = [c["email"] for c in broker_contacts(session, [e.broker_party_id]).get(
            e.broker_party_id, []) if c.get("email")]
        raiser = (raised_by or "").strip().lower()
        reply = next((a for a in admins if a.strip().lower() == raiser),
                     admins[0] if admins else None)
        notify_people(
            carrier_admin_recipients(e.tenant_id),
            f"{broker_name} disputed a late bordereau",
            body=("The broker says this file should not be counted as late. "
                  "Accept or reject it on the Bordereau Calendar — nothing "
                  "more happens to it until you do."),
            facts=[("Programme", program_name), ("Period", e.period),
                   ("Due", _iso(e.due_date)),
                   ("Reason", DISPUTE_REASONS.get(e.dispute_reason or "", "—")),
                   ("Their note", e.dispute_note or "—")],
            link_path=f"/bordereau-calendar?month={_iso(e.due_date)[:7]}",
            link_label="Open the Bordereau Calendar",
            footer=CARRIER_ADMIN_FOOTER, cc=admins, reply_to=reply)
    except Exception:  # noqa: BLE001 — telling never undoes the act
        pass


_DECISION_TITLES = {
    "accept": "Your dispute was accepted",
    "reject": "Your dispute was not accepted — please send the file",
    "waive": "No file is needed for this period",
    "extend": "You have more time to send this bordereau",
    "reopen": "Your late bordereau case was reopened — please send the file",
}


def notify_decision(session, e, decision: str, note: Optional[str],
                    program_name: str, carrier_name: str,
                    decided_by: Optional[str] = None) -> None:
    """Mail the broker's contacts the carrier's decision. Never raises.

    The carrier admin who decided is copied, so they hold exactly what the
    broker was told (as Send Reminder does), and a reply reaches them rather
    than the NOTIFY mailbox."""
    try:
        from notifications import notify_people
        from submission_calendar_service import broker_contacts
        people = [{"email": c["email"], "name": c.get("name")}
                  for c in broker_contacts(session, [e.broker_party_id]).get(
                      e.broker_party_id, []) if c.get("email")]
        facts = [("Carrier", carrier_name), ("Programme", program_name),
                 ("Period", e.period), ("Due", _iso(e.due_date))]
        if decision == "extend" and e.case_until:
            facts.append(("New date", _iso(e.case_until)))
        facts.append(("Their note", note or "—"))
        notify_people(
            people, _DECISION_TITLES.get(decision, "Your late bordereau was updated"),
            facts=facts, link_path="/broker/calendar",
            link_label="Open your Bordereau Calendar",
            footer=("You're receiving this because you're a contact for this "
                    "broker on Kavachio."),
            cc=[decided_by] if decided_by else None, reply_to=decided_by)
    except Exception:  # noqa: BLE001
        pass
