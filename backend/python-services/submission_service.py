"""The broker exception loop — tell the broker, let them fix it, deliver it.

A broker who sends a file by API, email or SFTP is not sitting in our portal,
so the result has to go BACK to them — on the channel they used and by email —
with a way to fix it.

No tables of its own. A SUBMISSION is the files a broker sent for one thing
(file_arrival rows sharing `submission_ref`) plus any corrections they made on
the secure link (output_exports rows with `version_status` set):

  file_arrival.submission_ref     the reference the broker quotes — the
                                  public_ref of the thread's FIRST file
  file_arrival.version_no         v1, v2 … for a file
  output_exports.version_no       … and for a secure-link correction
  version_status / version_note   where that version stands
  first file's deadline_at /      the thread's deadline and delivery
  delivered_at
  program.delivery_rule           what holds a file back, and the deadline
  intake_route.notify_emails      extra people told about a channel's files
  activity_events                 every message sent (bdx_notice_*), every
                                  delivery (with what was still open), and the
                                  secure link's codes and answers

  on_land()            a file arrived (any channel): find its submission or
                       start one, add a version, send a receipt when there is
                       something to say (refused, on hold, SFTP receipt)
  on_run_outcome()     the file was processed: count what is open, decide
                       delivery, tell the broker
  on_export_rerendered()  the portal's Fix & Validate re-checked a file in place
  validate_corrections()  the secure link's Validate — every rule over the
                       corrected data, as a throwaway self-check
  submit_corrections()   the secure link's Submit — the corrected data is SENT
                       like a file on the broker's channel and becomes the next
                       version; nothing already received is edited
  deliver()            the file goes to the carrier (send_bordereau)
  sweep_deadlines()    the programme's deadline rule, applied automatically

What HOLDS a file is the programme's delivery rule: by default an open
exception from a rule whose own severity is critical holds the file, anything
else travels with it. At the deadline the rule either delivers the file flagged
(recording what was still open) or keeps it on hold for the carrier to decide.

Every public function is best-effort from its caller's point of view: the loop
must never be the reason a file fails to land or to process.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import event, or_, text

from intake_models import FileArrival, IntakeRoute

log = logging.getLogger("kavachio.submissions")

# The reference is file_arrival.public_ref (`inb_` + 15 Crockford base32).
# Not \b: in "Wani_inb_01….xlsx" the underscore is a word character.
REF_RE = re.compile(r"(?<![A-Za-z0-9])inb_([0-9A-Za-z]{15})(?![0-9A-Za-z])",
                    re.IGNORECASE)

DEFAULT_RULE = {
    "hold_severities": ["critical"],
    "rule_overrides": {},
    "correction_days": 5,
    "deadline_action": "deliver_flagged",
}
DEADLINE_ACTIONS = ("deliver_flagged", "keep_on_hold")
SEVERITIES = ("critical", "warning", "info")

LINK_DAYS = 14

# Words the broker and carrier both see. One vocabulary on every channel.
STATUS_WORDS = {
    "received": "Received",
    "processing": "Processing",
    "clean": "Clean",
    "with_exceptions": "With Exceptions",
    "failed": "Failed",
    "on_hold": "On Hold — No action required",
    "rejected": "Rejected",
    "duplicate": "Already received",
    "ready": "Ready to deliver",
    "delivered": "Delivered",
    "delivered_flagged": "Delivered — unresolved exceptions",
    "held_at_deadline": "On Hold — deadline passed",
    "superseded": "Superseded",
}
# A submission the broker can still answer on the secure link.
OPEN_STATUSES = ("with_exceptions", "ready", "held_at_deadline")
_NOT_A_TURN = ("rejected", "duplicate")   # versions that never become "current"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    dt = _aware(dt)
    return dt.isoformat() if dt else None


def find_reference(*texts: Optional[str]) -> Optional[str]:
    """The first submission reference quoted in a file name, a subject or a field."""
    for t in texts:
        m = REF_RE.search(t or "")
        if m:
            return "inb_" + m.group(1).upper()
    return None


def notifications_enabled() -> bool:
    """Broker emails are OFF unless BROKER_NOTIFY_ENABLED=1 — the same default
    as the carrier auto-send, so a dev server on shared data mails nobody."""
    return os.getenv("BROKER_NOTIFY_ENABLED", "0").strip().lower() in (
        "1", "true", "yes", "on")


# ── the delivery rule (program.delivery_rule) ───────────────────────────────

def rule_for(s, program_id: Optional[int]) -> dict:
    """The programme's delivery rule, with defaults for anything unset."""
    rule = dict(DEFAULT_RULE)
    rule["rule_overrides"] = {}
    if program_id is None:
        return rule
    from db import Program
    prog = s.get(Program, program_id)
    row = (prog.delivery_rule if prog is not None else None) or {}
    if not isinstance(row, dict):
        return rule
    if row.get("hold_severities") is not None:
        rule["hold_severities"] = [str(x).lower() for x in row["hold_severities"]
                                   if str(x).lower() in SEVERITIES]
    if row.get("rule_overrides"):
        rule["rule_overrides"] = {str(k): v for k, v in row["rule_overrides"].items()
                                  if v in ("hold", "pass")}
    if row.get("correction_days"):
        rule["correction_days"] = max(1, int(row["correction_days"]))
    if row.get("deadline_action") in DEADLINE_ACTIONS:
        rule["deadline_action"] = row["deadline_action"]
    return rule


def holds(e: dict, rule: dict) -> bool:
    """Does this OPEN exception hold the file? The rule's own severity decides,
    unless the programme overrides that one rule."""
    rid = e.get("rule_id")
    if rid is not None:
        ov = rule["rule_overrides"].get(str(rid))
        if ov:
            return ov == "hold"
    return str(e.get("severity") or "").lower() in rule["hold_severities"]


# ── what is open on a file ──────────────────────────────────────────────────

def _decorated(export) -> list[dict]:
    """The export's countable exceptions with decisions + recommendations
    attached — exactly what the portal's exception screen reads."""
    from broker_tally import countable
    excs = countable(list(export.exceptions or []))
    if not excs:
        return []
    try:
        import main as _m
        excs = _m._attach_recommendations(excs)
        excs = _m._attach_decisions(excs, export)
    except Exception:  # noqa: BLE001 — counts still work without decoration
        log.warning("could not decorate exceptions of export %s", export.id,
                    exc_info=True)
    return [e for e in excs if isinstance(e, dict)]


def _total(export) -> int:
    from broker_tally import countable
    return len(countable(list(export.exceptions or []))) if export is not None else 0


def export_state(s, export, rule: Optional[dict] = None) -> dict:
    from broker_tally import settled
    rule = rule or rule_for(s, export.program_id)
    excs = _decorated(export)
    open_ = [e for e in excs if not settled(e)]
    blocking = [e for e in open_ if holds(e, rule)]
    return {"total": len(excs), "open": len(open_), "blocking_open": len(blocking),
            "exceptions": excs, "open_list": open_, "rule": rule}


def delivery_hold(s, out) -> Optional[str]:
    """Called by the render just before it would send a file to the carrier:
    a reason to hold it, or None to let it go."""
    try:
        if (out.status or "") == "not_validated":
            return "the file could not be checked"
        st = export_state(s, out)
        if st["blocking_open"]:
            return (f"{st['blocking_open']} exception(s) the programme's delivery "
                    f"rule holds back are still open")
    except Exception:  # noqa: BLE001 — never block delivery on our own error
        log.warning("delivery check failed for export %s", out.id, exc_info=True)
    return None


# ── classifying problems: whose move is it? ─────────────────────────────────

# A run that stops for one of these is the CARRIER's (or our) set-up, never
# the broker's file — checked first, because their wording also mentions
# columns and templates.
_OUR_SIDE_RUN_ERRORS = ("setup", "set-up", "pipeline", "output template",
                        "contract", "not linked to a programme", "no copy",
                        "stopped unexpectedly", "mapping")
# …and these are about the file itself: the broker can fix and resend.
_BROKER_FIXABLE_RUN_ERRORS = ("no readable sheets", "more than one table",
                              "multiple tables", "header row", "could not be opened",
                              "could not be read")


def _classify_failure(error: Optional[str]) -> tuple[str, str]:
    """(status, note) for a run that did not produce a checked file."""
    msg = (error or "").strip()
    low = msg.lower()
    if not any(k in low for k in _OUR_SIDE_RUN_ERRORS) and \
            any(k in low for k in _BROKER_FIXABLE_RUN_ERRORS):
        return "failed", msg or "The file could not be read."
    return "on_hold", ("We are looking into a problem on our side. "
                       "No action is needed from you.")


def _classify_held(reason: str) -> tuple[str, str]:
    low = (reason or "").lower()
    plain = re.sub(r"^held\s*—\s*", "", reason or "", flags=re.I)
    plain = plain[:1].upper() + plain[1:]
    if "same file" in low:
        return "duplicate", ("We already have this exact file, so it was not "
                             "processed again. If you meant to send a correction, "
                             "change the data and send it again.")
    if "no rows" in low or "column" in low:
        return "failed", plain
    return "on_hold", "The carrier needs to finish its set-up. No action is needed from you."


def _classify_not_validated(export) -> tuple[str, str]:
    reason = ""
    for e in export.exceptions or []:
        if isinstance(e, dict) and e.get("error_class") == "not_validated" \
                and e.get("rule_id") is None:
            reason = str(e.get("message") or e.get("reason") or "")
            break
    low = reason.lower()
    if "sheet" in low or "set aside" in low:
        return "failed", reason or "The file could not be checked."
    return "on_hold", "We could not finish checking this file. No action is needed from you."


# ── the submission thread, read from file_arrival + output_exports ─────────

class Version:
    """One version: a FILE (file_arrival row) or a secure-link CORRECTION
    (output_exports row). Status lives on whichever row the version is."""

    def __init__(self, no: int, *, arrival=None, export=None, landing_id=None,
                 route_channel: Optional[str] = None):
        self.no = no
        self.arrival = arrival
        self.export = export          # set for a correction only
        self._landing_id = landing_id
        self._route_channel = route_channel   # a channel file's way in lives on its route

    @property
    def row(self):
        return self.arrival if self.arrival is not None else self.export

    @property
    def is_file(self) -> bool:
        return self.arrival is not None

    @property
    def source(self) -> str:
        if not self.is_file:
            return "secure_link"
        a = self.arrival
        if a.matched_by == "secure_link":
            return "secure_link"
        return (a.channel or self._route_channel
                or ("upload" if a.route_id is None else "channel"))

    @property
    def status(self) -> str:
        return self.row.version_status or "received"

    @property
    def note(self) -> Optional[str]:
        return self.row.version_note

    def set(self, status: str, note: Optional[str] = None) -> None:
        self.row.version_status = status
        self.row.version_note = note

    @property
    def export_id(self) -> Optional[int]:
        return self.arrival.run_export_id if self.is_file else self.export.id

    @property
    def landing_id(self) -> Optional[int]:
        return self.arrival.run_landing_id if self.is_file else self._landing_id

    @property
    def created_at(self):
        return self.arrival.received_at if self.is_file else self.export.created_at


class Thread:
    """A submission: its files and corrections, oldest first."""

    def __init__(self, ref: str, files: list, corrections: list, route=None,
                 channels: Optional[dict] = None):
        self.ref = ref
        self.files = files
        self.head = next((f for f in files if f.public_ref == ref), files[0])
        self.route = route            # the latest file's channel, if one had one
        vers, landing = [], None
        merged = sorted([(f.version_no or 0, 0, f) for f in files]
                        + [(x.version_no or 0, 1, x) for x in corrections],
                        key=lambda t: (t[0], t[1]))
        for no, kind, row in merged:
            if kind == 0:
                vers.append(Version(no, arrival=row,
                                    route_channel=(channels or {}).get(row.route_id)))
                landing = row.run_landing_id or landing
            else:
                # A correction is an overlay on the landing of the file before it.
                vers.append(Version(no, export=row, landing_id=landing))
        self.versions = vers

    # identity -----------------------------------------------------------
    @property
    def tenant_id(self):
        return self.head.tenant_id

    @property
    def broker_party_id(self):
        return self.head.matched_broker_party_id

    @property
    def route_id(self):
        return self.route.id if self.route is not None else self.head.route_id

    def _latest(self, attr):
        for f in reversed(self.files):
            v = getattr(f, attr, None)
            if v is not None:
                return v
        return None

    @property
    def program_id(self):
        return self._latest("program_id") or (
            self.route.program_id if self.route is not None else None)

    @property
    def period(self):
        return self._latest("reporting_period")

    @property
    def contract_id(self):
        return self._latest("contract_id")

    @property
    def channel(self) -> str:
        if self.route is not None and self.route.channel:
            return self.route.channel
        return self.head.channel or "upload"

    # state --------------------------------------------------------------
    @property
    def current(self) -> Version:
        turns = [v for v in self.versions if v.status not in _NOT_A_TURN]
        return (turns or self.versions)[-1]

    @property
    def status(self) -> str:
        return self.current.status

    @property
    def note(self) -> Optional[str]:
        return self.current.note

    @property
    def current_export_id(self) -> Optional[int]:
        return self.current.export_id

    @property
    def file_name(self) -> str:
        """How the portal names this submission: its latest file."""
        return self.files[-1].filename or "Bordereau"

    @property
    def deadline_at(self):
        return self.head.deadline_at

    @property
    def delivered_at(self):
        return self.head.delivered_at

    def version(self, no: int) -> Optional[Version]:
        return next((v for v in self.versions if v.no == no), None)

    def by_export(self, export_id: int) -> Optional[Version]:
        return next((v for v in reversed(self.versions) if v.export_id == export_id), None)


def thread_ref(s, ref: Optional[str]) -> Optional[str]:
    """The submission a reference points at. A sender is handed only their
    FILE's reference, so either one is taken: a submission's own, or a file's,
    which leads to the submission that file is a version of."""
    ref = find_reference(ref)
    if not ref:
        return None
    if s.query(FileArrival.id).filter(FileArrival.submission_ref == ref).first():
        return ref
    row = (s.query(FileArrival.submission_ref)
           .filter(FileArrival.public_ref == ref).first())
    return row.submission_ref if row and row.submission_ref else None


def load(s, ref: Optional[str]) -> Optional[Thread]:
    if not ref:
        return None
    from db import OutputExport
    files = (s.query(FileArrival).filter(FileArrival.submission_ref == ref)
             .order_by(FileArrival.version_no, FileArrival.id).all())
    if not files:
        return None
    corrections = (s.query(OutputExport)
                   .filter(OutputExport.submission_ref == ref,
                           OutputExport.version_status.isnot(None))
                   .order_by(OutputExport.version_no).all())
    # The channel the submission answers on: its LATEST file's. A September
    # file sent by hand and corrected over SFTP is told the result in the SFTP
    # folder, where the broker's system is now looking.
    by_channel = next((f for f in reversed(files) if f.route_id), None)
    route = s.get(IntakeRoute, by_channel.route_id) if by_channel else None
    route_ids = {f.route_id for f in files if f.route_id}
    channels = (dict(s.query(IntakeRoute.id, IntakeRoute.channel)
                     .filter(IntakeRoute.id.in_(route_ids)).all()) if route_ids else {})
    return Thread(ref, files, corrections, route, channels)


def period_of(s, arrival) -> Optional[str]:
    """The period a file is for: its own, or the one its submission is for."""
    if arrival.reporting_period:
        return arrival.reporting_period
    if not arrival.submission_ref:
        return None
    return (s.query(FileArrival.reporting_period)
            .filter(FileArrival.submission_ref == arrival.submission_ref,
                    FileArrival.reporting_period.isnot(None))
            .order_by(FileArrival.id.desc()).limit(1).scalar())


def thread_for_arrival(s, arrival_id: int) -> tuple[Optional[Thread], Optional[Version]]:
    a = s.get(FileArrival, arrival_id)
    th = load(s, a.submission_ref) if a is not None else None
    if th is None:
        return None, None
    return th, th.version(a.version_no)


def _lock(s, ref: str) -> None:
    """Serialise version numbering for one submission (Postgres only)."""
    try:
        if s.get_bind().dialect.name == "postgresql":
            s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:r))"), {"r": ref})
    except Exception:  # noqa: BLE001
        pass


def _next_version_no(s, ref: str) -> int:
    _lock(s, ref)
    n = s.execute(text(
        "SELECT GREATEST("
        " (SELECT COALESCE(MAX(version_no), 0) FROM file_arrival WHERE submission_ref = :r),"
        " (SELECT COALESCE(MAX(version_no), 0) FROM output_exports"
        "   WHERE submission_ref = :r AND version_status IS NOT NULL))"),
        {"r": ref}).scalar()
    return int(n or 0) + 1


# ── recipients ──────────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def route_contacts(route) -> list[str]:
    emails = getattr(route, "notify_emails", None) or [] if route is not None else []
    return [e for e in emails if _EMAIL_RE.match(str(e))]


def recipients(s, th: Thread, arrival=None) -> list[str]:
    """Who hears about this submission: the channel's notify list, the person
    or address that sent the file, and — only when we know nobody — the
    broker's own login."""
    route = s.get(IntakeRoute, th.route_id) if th.route_id else None
    out: list[str] = list(route_contacts(route))
    if arrival is None:
        # A correction made on the secure link has no file of its own: whoever
        # sent the latest file still hears about it.
        arrival = th.files[-1]
    sender = (arrival.claimed_sender or "").strip()
    if _EMAIL_RE.match(sender):
        out.append(sender)
    if arrival.submitted_by_user_id:
        from db import AppUser
        u = s.get(AppUser, arrival.submitted_by_user_id)
        if u is not None and u.email:
            out.append(u.email)
    if not out and th.broker_party_id:
        from submission_calendar_service import broker_contacts
        out = [c["email"] for c in broker_contacts(s, [th.broker_party_id])
               .get(th.broker_party_id, [])]
    seen, uniq = set(), []
    for e in out:
        k = e.strip().lower()
        if k and k not in seen:
            seen.add(k)
            uniq.append(e.strip())
    return uniq


# ── a file arrived ──────────────────────────────────────────────────────────

def on_land(s, arrival, route=None, *, period: Optional[str] = None,
            program_id: Optional[int] = None, replaces: Optional[str] = None,
            hint: Optional[str] = None) -> Optional[Thread]:
    """A file arrived. Caller's session; flushed, not committed."""
    try:
        with s.begin_nested():
            return _on_land(s, arrival, route, period=period, program_id=program_id,
                            replaces=replaces, hint=hint)
    except Exception:  # noqa: BLE001 — a file must always land
        log.warning("submission bookkeeping failed for arrival %s",
                    getattr(arrival, "public_ref", None), exc_info=True)
        return None


def _match(s, arrival, *, broker, program_id, period, reference) -> tuple[Optional[Thread], str]:
    if reference:
        th = load(s, thread_ref(s, reference))
        if th is not None and th.tenant_id == arrival.tenant_id \
                and th.broker_party_id == broker:
            return th, "reference"
        log.info("reference %s quoted by broker %s matches none of theirs",
                 reference, broker)
    if program_id and period:
        # The same broker, programme, CONTRACT and period — whichever way the
        # earlier file came in: a Sept file sent by hand, then a Sept file by
        # API, are versions 1 and 2 of one submission. A broker with two
        # contracts on a programme sends two bordereaux a period, and they are
        # two submissions. A file from before contracts were kept on files
        # (no contract) still matches, behind one that agrees.
        from sqlalchemy import case, func
        q = (s.query(FileArrival.submission_ref)
             .outerjoin(IntakeRoute, IntakeRoute.id == FileArrival.route_id)
             .filter(FileArrival.tenant_id == arrival.tenant_id,
                     FileArrival.matched_broker_party_id == broker,
                     func.coalesce(FileArrival.program_id,
                                   IntakeRoute.program_id) == program_id,
                     FileArrival.reporting_period == period,
                     FileArrival.submission_ref.isnot(None),
                     FileArrival.id != arrival.id))
        cid = getattr(arrival, "contract_id", None)
        if cid is not None:
            q = (q.filter(or_(FileArrival.contract_id == cid,
                              FileArrival.contract_id.is_(None)))
                 .order_by(case((FileArrival.contract_id == cid, 0), else_=1)))
        prior = q.order_by(FileArrival.id.desc()).first()
        if prior is not None:
            th = load(s, prior[0])
            if th is not None:
                return th, "period"
    return None, "new"


def _on_land(s, arrival, route, *, period, program_id, replaces, hint):
    broker = arrival.matched_broker_party_id
    if broker is None:
        return None          # an unknown sender: nobody to tell, nothing to match
    # Read-only on what the intake already decided: the file's channel,
    # programme and run are exactly what they were. Only the period (a new
    # column) is filled in when the caller knows it.
    if period and not arrival.reporting_period:
        arrival.reporting_period = period
    program_id = program_id or arrival.program_id or (
        route.program_id if route is not None else None)
    ref = find_reference(replaces, arrival.filename, hint)

    th, matched_by = None, "new"
    if arrival.outcome == "held" and "same file" in (arrival.turned_away_reason or "").lower():
        # The exact bytes we already have: it belongs to THAT file's submission,
        # as "already received" — never a submission of its own.
        # …and only a copy for the SAME programme and month: identical bytes
        # filed for another programme are not that programme's next version.
        from sqlalchemy import func
        q = (s.query(FileArrival.submission_ref)
             .outerjoin(IntakeRoute, IntakeRoute.id == FileArrival.route_id)
             .filter(FileArrival.tenant_id == arrival.tenant_id,
                     FileArrival.matched_broker_party_id == broker,
                     FileArrival.file_hash_sha256 == arrival.file_hash_sha256,
                     FileArrival.submission_ref.isnot(None),
                     FileArrival.id != arrival.id))
        if program_id is not None and arrival.reporting_period:
            q = q.filter(func.coalesce(FileArrival.program_id, IntakeRoute.program_id) == program_id,
                         FileArrival.reporting_period == arrival.reporting_period)
        prior = q.order_by(FileArrival.id.desc()).first()
        if prior is not None:
            th, matched_by = load(s, prior[0]), "duplicate"
    if th is None:
        th, matched_by = _match(s, arrival, broker=broker, program_id=program_id,
                                period=arrival.reporting_period, reference=ref)

    if th is None:
        arrival.submission_ref = arrival.public_ref
        arrival.version_no = 1
    else:
        arrival.submission_ref = th.ref
        arrival.version_no = _next_version_no(s, th.ref)
        if matched_by != "duplicate" and th.status in ("delivered", "delivered_flagged") \
                and arrival.outcome == "accepted":
            # A new file after delivery opens a new round: a new deadline, and
            # the next delivery is a new one.
            th.head.delivered_at = None
            th.head.deadline_at = None
    arrival.matched_by = matched_by

    if arrival.outcome == "accepted":
        st, note = "processing", None
    elif arrival.outcome == "turned_away":
        st, note = "rejected", arrival.turned_away_reason
    else:   # held
        st, note = _classify_held(arrival.turned_away_reason or "")
    arrival.version_status, arrival.version_note = st, note
    s.flush()

    th = load(s, arrival.submission_ref)
    ver = th.version(arrival.version_no)
    # What the broker hears straight away. A processed file's result follows
    # within minutes, so an accepted file only gets the SFTP receipt (a folder
    # is silent otherwise); refusals and holds are told now.
    if th.channel == "sftp" and route is not None:
        _queue_sftp(s, th, ver, route, "receipt", _receipt_doc(th, ver, arrival))
    if st in ("rejected", "failed", "on_hold", "duplicate"):
        _queue_result_email(s, th, ver, arrival, event=st)
        if th.channel == "sftp" and route is not None:
            _queue_sftp(s, th, ver, route, "result", status_json(s, th))
    return th


# ── the file was processed ──────────────────────────────────────────────────

def on_run_outcome(arrival_id: int) -> None:
    """intake_service.mark_run recorded how a run went (done / failed /
    not_run). Own session, own commit; never raises."""
    from db import SessionLocal
    try:
        with SessionLocal() as s:
            th, ver = thread_for_arrival(s, arrival_id)
            if th is None or ver is None:
                return
            a = ver.arrival
            if a.run_state in (None, "running"):
                return
            if a.run_state == "done" and a.run_export_id:
                _settle(s, th, ver, a.run_export_id, arrival=a)
            elif a.run_state in ("failed", "not_run"):
                st, note = _classify_failure(a.run_error)
                ver.set(st, note)
                s.flush()
                th = load(s, th.ref)
                ver = th.version(ver.no)
                _queue_result_email(s, th, ver, a, event=st)
                _queue_sftp_for(s, th, ver, "result")
                if st == "on_hold":
                    _alert_carrier(s, th, ver, a.run_error)
            s.commit()
    except Exception:  # noqa: BLE001
        log.warning("submission outcome failed for arrival %s", arrival_id, exc_info=True)


def _link_calendar(s, arrival, program_id, export_id: int) -> None:
    """A file sent by email, SFTP or API ticked its period when it LANDED,
    before it was run, so its calendar version names no output: the
    calendar's Versions panel had no file to open, and delivery could not
    find the version to mark it sent. Name the output now that there is one."""
    from db import ExpectedSubmission, SubmissionVersion
    period, broker = arrival.reporting_period, arrival.matched_broker_party_id
    if not (program_id and period and broker):
        return
    if s.query(SubmissionVersion.id).filter(
            SubmissionVersion.received_export_id == export_id).first():
        return              # the run ticked the calendar itself (a hand upload)
    # THIS file's version: land_file writes it in the same transaction as the
    # arrival, so it is the one created at the moment the file landed. (By
    # name alone, a later version of the same file took the first one's.)
    from submission_calendar_service import _SAME_LANDING_SECONDS, _as_utc_naive
    landed = _as_utc_naive(arrival.received_at)
    if landed is None:
        return
    cands = (s.query(SubmissionVersion)
             .filter(SubmissionVersion.tenant_id == arrival.tenant_id,
                     SubmissionVersion.program_id == program_id,
                     SubmissionVersion.period == period,
                     or_(SubmissionVersion.broker_party_id == broker,
                         SubmissionVersion.broker_party_id.is_(None)),
                     SubmissionVersion.source_filename == arrival.filename,
                     SubmissionVersion.received_export_id.is_(None),
                     SubmissionVersion.created_at.isnot(None)).all())
    gap = lambda v: abs((v.created_at - landed).total_seconds())  # noqa: E731
    cal = min(cands, key=gap, default=None)
    if cal is None or gap(cal) > _SAME_LANDING_SECONDS:
        return
    cal.received_export_id = export_id
    if cal.version_no == 1:
        e = s.get(ExpectedSubmission, cal.expected_id)
        if e is not None and e.received_export_id is None:
            e.received_export_id = export_id
    s.flush()


def _settle(s, th: Thread, ver: Version, export_id: int, arrival=None,
            notify: bool = True) -> None:
    """A version has a checked file: count, decide delivery, tell the broker."""
    from db import OutputExport
    out = s.get(OutputExport, export_id)
    if out is None:
        return
    out.submission_ref, out.version_no = th.ref, ver.no
    if ver.is_file:
        _link_calendar(s, ver.arrival, th.program_id, export_id)
    if ver.is_file and not ver.arrival.reporting_period:
        # The run picked the period (from the file name, or the oldest open
        # one) — learn it, so a correction can find this submission by period.
        from db import SubmissionVersion
        cal = (s.query(SubmissionVersion)
               .filter(SubmissionVersion.received_export_id == export_id).first())
        if cal is not None:
            ver.arrival.reporting_period = cal.period
    if (out.status or "") == "not_validated":
        st, note = _classify_not_validated(out)
    else:
        state = export_state(s, out)
        st = "clean" if state["total"] == 0 else (
            "with_exceptions" if state["blocking_open"] else "ready")
        note = None
    ver.set(st, note)
    s.flush()

    th = load(s, th.ref)
    ver = th.version(ver.no)
    newest = th.current.no == ver.no
    if newest:
        for old in th.versions:
            if old.no != ver.no and old.status in ("processing", "clean",
                                                   "with_exceptions", "ready",
                                                   "failed", "on_hold"):
                old.set("superseded", old.note)
        if st == "with_exceptions" and th.head.deadline_at is None:
            days = rule_for(s, th.program_id)["correction_days"]
            th.head.deadline_at = _now() + timedelta(days=days)
    s.flush()

    if newest and st in ("clean", "ready"):
        deliver(s, th, actor="Kavachio (delivery rule met)", notify=False)
        th = load(s, th.ref)
        ver = th.version(ver.no)
    if notify:
        _queue_result_email(s, th, ver, arrival, event="result")
        _queue_sftp_for(s, th, ver, "result")
    if st == "on_hold":
        _alert_carrier(s, th, ver, note)


def on_export_rerendered(export_id: int) -> None:
    """The portal's Fix & Validate re-checked an export in place: refresh its
    version and deliver it if the rule is now met."""
    from db import OutputExport, SessionLocal
    try:
        with SessionLocal() as s:
            out = s.get(OutputExport, export_id)
            th = load(s, out.submission_ref) if out is not None else None
            if th is None or th.status in ("delivered", "delivered_flagged"):
                return
            ver = th.by_export(export_id)
            if ver is None:
                return
            _settle(s, th, ver, export_id, notify=False)
            s.commit()
    except Exception:  # noqa: BLE001
        log.warning("refresh after Fix & Validate failed for export %s", export_id,
                    exc_info=True)


# ── delivery ────────────────────────────────────────────────────────────────

def deliver(s, th: Thread, *, actor: str, flagged: bool = False,
            notify: bool = True) -> dict:
    """Send the CURRENT version to the carrier. Caller commits."""
    from db import ActivityEvent, OutputExport, SubmissionVersion
    cur = th.current
    if cur.export_id is None:
        return {"delivered": False, "reason": "nothing processed yet"}
    unresolved = None
    if flagged:
        out = s.get(OutputExport, cur.export_id)
        st = export_state(s, out) if out is not None else {"open_list": []}
        unresolved = [_brief(e) for e in st["open_list"]][:500]

    sent = None
    cal = (s.query(SubmissionVersion)
           .filter(SubmissionVersion.received_export_id == cur.export_id)
           .order_by(SubmissionVersion.id.desc()).first())
    if cal is not None and cal.released_at is None and th.broker_party_id:
        try:
            from submission_calendar_service import send_bordereau
            with s.begin_nested():
                sent = send_bordereau(s, cal.expected_id, th.broker_party_id,
                                      actor_email=actor)
        except Exception as exc:  # noqa: BLE001 — recorded, never raised
            sent = {"mail_sent": False, "mail_error": str(exc)}
            log.info("delivery of %s: %s", th.ref, exc)
    elif cal is not None:
        sent = {"already_sent": True}

    cur.set("delivered_flagged" if flagged else "delivered")
    th.head.delivered_at = _now()
    # What was still open travels with the delivery record, so "what did the
    # carrier accept?" stays answerable.
    s.add(ActivityEvent(
        tenant_id=th.tenant_id, actor=actor,
        action="bdx_submission_delivered",
        target=f"submission:{th.ref}",
        details={"reference": th.ref, "version": cur.no, "flagged": flagged,
                 "unresolved_count": len(unresolved or []),
                 "unresolved": unresolved,
                 "calendar": bool(cal), "mail": sent}))
    s.flush()
    if notify:
        th = load(s, th.ref)
        _queue_result_email(s, th, None, None, event="delivered")
        _queue_sftp_for(s, th, None, "result")
    return {"delivered": True, "flagged": flagged, "send": sent}


def sweep_deadlines() -> int:
    """Apply each programme's deadline rule to submissions still waiting on the
    broker. Returns how many it acted on."""
    from db import SessionLocal
    n = 0
    with SessionLocal() as s:
        heads = (s.query(FileArrival.submission_ref)
                 .filter(FileArrival.deadline_at.isnot(None),
                         FileArrival.deadline_at <= _now(),
                         FileArrival.delivered_at.is_(None),
                         FileArrival.submission_ref == FileArrival.public_ref)
                 .limit(200).all())
        for (ref,) in heads:
            try:
                th = load(s, ref)
                if th is None or th.status != "with_exceptions":
                    continue
                rule = rule_for(s, th.program_id)
                if rule["deadline_action"] == "deliver_flagged":
                    deliver(s, th, actor="Kavachio (deadline rule)", flagged=True)
                else:
                    note = ("The correction deadline passed. The carrier decides "
                            "whether to accept the file as it is.")
                    th.current.set("held_at_deadline", note)
                    s.flush()
                    th = load(s, ref)
                    _alert_carrier(s, th, None, note, event="deadline_hold")
                    _queue_result_email(s, th, None, None, event="deadline_hold")
                s.commit()
                n += 1
            except Exception:  # noqa: BLE001
                s.rollback()
                log.warning("deadline rule failed for %s", ref, exc_info=True)
    return n


def _sweep_every() -> int:
    """Seconds between deadline sweeps; 0 switches the sweep off."""
    try:
        return int(os.environ.get("SUBMISSION_DEADLINE_SWEEP_SECONDS", "300"))
    except ValueError:
        return 300


def start(app) -> None:
    """Attach the deadline sweep to the app, the same way intake_review does."""
    import asyncio
    every = _sweep_every()
    if every <= 0:
        log.info("submission deadline sweep off (SUBMISSION_DEADLINE_SWEEP_SECONDS=0)")
        return
    every = max(10, every)
    state = {"task": None}

    async def _loop() -> None:
        from starlette.concurrency import run_in_threadpool
        while True:
            await asyncio.sleep(every)
            try:
                await run_in_threadpool(sweep_deadlines)
            except Exception:  # noqa: BLE001
                log.warning("deadline sweep failed", exc_info=True)

    @app.on_event("startup")
    async def _start_sweep() -> None:         # pragma: no cover - wiring
        if state["task"] is None or state["task"].done():
            state["task"] = asyncio.create_task(_loop())

    @app.on_event("shutdown")
    async def _stop_sweep() -> None:          # pragma: no cover - wiring
        if state["task"] is not None:
            state["task"].cancel()
            state["task"] = None


# ── exception report: in the broker's own terms ─────────────────────────────

def _brief(e: dict) -> dict:
    return {"sheet": e.get("sheet"), "row": e.get("row"),
            "field": e.get("field") or e.get("column"),
            "rule": e.get("rule_name") or e.get("code"),
            "severity": e.get("severity"), "value": e.get("actual_value"),
            "policy_number": e.get("policy_number")}


def _landing_context(s, landing_id: Optional[int]):
    if not landing_id:
        return None
    row = s.execute(text(
        "SELECT lr.data, f.sheet_routing, f.column_mapping FROM landing_record lr "
        "LEFT JOIN direct_format f ON f.id = lr.format_id WHERE lr.id = :l"),
        {"l": landing_id}).mappings().first()
    if not row:
        return None

    def _d(v):
        return v if isinstance(v, dict) else (json.loads(v) if v else {})
    return _d(row["data"]), _d(row["sheet_routing"]), _d(row["column_mapping"])


def _field_of(e: dict) -> Optional[str]:
    return e.get("field") or e.get("column")


def _what_to_fix(e: dict) -> str:
    """One plain sentence: what the value must be. The rule explainer gives a
    small structure (problem / requirement / also accepted); flatten it."""
    exp = e.get("explanation")
    if isinstance(exp, dict):
        parts = [exp.get("requirement"), exp.get("problem")]
        also = exp.get("accepts_also")
        if also:
            parts.append(f"Also accepted: {also}.")
        text_ = " ".join(str(p).strip() for p in parts if p)
        if text_:
            return text_
    elif isinstance(exp, str) and exp.strip():
        return exp.strip()
    rec = e.get("recommendation")
    if isinstance(rec, str) and rec.strip():
        return f"Expected: {rec.strip()}"
    return str(e.get("reason") or e.get("message") or "")


def exception_key(e: dict) -> str:
    return f"{e.get('sheet')}|{e.get('row')}|{_field_of(e)}"


def _num(v) -> Optional[float]:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _fmt_num(n: Optional[float]) -> str:
    if n is None:
        return ""
    return f"{n:,.0f}" if float(n).is_integer() else f"{n:,.2f}"


def _aggregate_of(e: dict, column) -> Optional[dict]:
    """An aggregate rule reports a TOTAL — a whole column added up — pinned to
    the first row that fed it. Shown as "107700.0" under "your value" it
    matches no cell in the file. Say what the number is, against its limit."""
    a = e.get("aggregate")
    if not isinstance(a, dict):
        return None
    fn = str(a.get("function") or "sum").lower()
    whole_file = a.get("level") == "file"
    total = _num(e.get("actual_value"))
    hi, lo = _num(a.get("max")), _num(a.get("min"))
    col = column or "This column"
    where = ("across the file" if whole_file
             else f"for policy {e['policy_number']}" if e.get("policy_number")
             else "for this policy")
    head = (f"{_fmt_num(total)} rows counted {where}" if fn == "count"
            else f"{_fmt_num(total)} different values of {col} {where}" if fn == "distinct_count"
            else f"{col} adds up to {_fmt_num(total)} {where}")
    over = total - hi if total is not None and hi is not None and total > hi else None
    under = lo - total if total is not None and lo is not None and total < lo else None
    tail = (f" — {_fmt_num(over)} over the {_fmt_num(hi)} limit." if over is not None
            else f" — {_fmt_num(under)} under the {_fmt_num(lo)} minimum." if under is not None
            else ".")
    label = ("Rows counted" if fn == "count" else "Values found" if fn == "distinct_count"
             else "File total" if whole_file else "Policy total")
    return {"label": label, "whole_file": whole_file, "total": total,
            "limit": hi if hi is not None else lo, "over_by": over if over is not None else under,
            "sentence": head + tail}


def _shown_value(r: dict) -> str:
    """The value column of the CSV and the email, in words for a total."""
    v = r.get("current_value")
    if r.get("value_label"):
        return f"{r['value_label']} {_fmt_num(_num(v))}"
    return "" if v is None else str(v)


def _shown_row(r: dict) -> str:
    if r.get("row") is None and r.get("value_label"):
        return "All rows"
    return "" if r.get("row") is None else str(r["row"])


def report_rows(s, export, landing_id: Optional[int], rule: Optional[dict] = None,
                decorated: Optional[list] = None) -> list[dict]:
    """One row per exception, located in the BROKER's file: their sheet, their
    data row and their column header, with the value sent, what was expected
    and what to fix."""
    from broker_tally import settled
    import direct_lane as dl
    rule = rule or rule_for(s, export.program_id)
    ctx = _landing_context(s, landing_id)
    rows = []
    for e in (decorated if decorated is not None else _decorated(export)):
        field = _field_of(e)
        sheet_in, row_in, col_in = e.get("sheet"), e.get("row"), field
        located = False
        if ctx and e.get("row") is not None and field:
            try:
                r = dl.resolve_landing_cell(ctx[0], ctx[1], ctx[2], e.get("sheet"),
                                            int(e.get("row")), field)
                if r.get("ok"):
                    sheet_in = r["input_sheet"]
                    row_in = int(r["input_row_index"]) + 1
                    col_in = r["source_column"]
                    located = True
            except Exception:  # noqa: BLE001
                pass
        expected = e.get("expected_value") or e.get("recommendation")
        if isinstance(expected, (list, dict)):
            expected = json.dumps(expected)
        st = str(e.get("status") or "").lower()
        agg = _aggregate_of(e, col_in)
        rows.append({
            "key": exception_key(e),
            "severity": str(e.get("severity") or "").lower() or "warning",
            "holds_file": holds(e, rule),
            "status": "open" if not settled(e) else st,
            "answer_note": e.get("resolution_note"),
            "rule": e.get("rule_name") or e.get("code") or "Check",
            "message": e.get("message") or e.get("reason") or "",
            # A file total belongs to no one row.
            "sheet": sheet_in, "row": None if agg and agg["whole_file"] else row_in,
            "column": col_in,
            "located_in_your_file": located,
            "output_sheet": e.get("sheet"), "output_row": e.get("row"),
            "output_field": field,
            "policy_number": e.get("policy_number"),
            "current_value": (agg["total"] if agg and agg["total"] is not None
                              else e.get("actual_value")),
            "value_label": agg["label"] if agg else None,
            "limit": agg["limit"] if agg else None,
            "over_by": agg["over_by"] if agg else None,
            "expected_value": expected if expected not in ("", None) else None,
            "what_to_fix": agg["sentence"] if agg else str(_what_to_fix(e))[:600],
            "rule_id": e.get("rule_id"),
        })
    rows.sort(key=lambda r: (r["status"] != "open", not r["holds_file"],
                             str(r["sheet"]), r["row"] or 0))
    return rows


REPORT_COLUMNS = (("version", "Version"),
                  ("severity", "Severity"), ("holds_file", "Holds the file"),
                  ("status", "Status"), ("sheet", "Sheet"), ("row", "Row"),
                  ("column", "Column"), ("policy_number", "Policy"),
                  ("current_value", "Current value"),
                  ("expected_value", "Expected value"),
                  ("what_to_fix", "What needs fixing"), ("rule", "Check"))


def report_csv(ref: str, version_no, rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([label for _, label in REPORT_COLUMNS])
    for r in rows:
        vals = []
        for k, _ in REPORT_COLUMNS:
            v = (ref if k == "reference" else
                 f"v{version_no}" if k == "version" else r.get(k))
            if k == "holds_file":
                v = "Yes" if v else "No"
            elif k == "row":
                v = _shown_row(r)
            elif k == "current_value":
                v = _shown_value(r)
            vals.append("" if v is None else v)
        w.writerow(vals)
    return buf.getvalue()


def _baseline(s, th: Thread) -> int:
    """Exceptions on the FIRST checked version — only the fallback total
    before the current version has been checked (see progress)."""
    from db import OutputExport
    for v in th.versions:
        if v.export_id and v.status not in _NOT_A_TURN:
            return _total(s.get(OutputExport, v.export_id))
    return 0


def progress(s, th: Thread, state: Optional[dict] = None) -> dict:
    from db import OutputExport
    base = _baseline(s, th)
    if state is None and th.current_export_id:
        out = s.get(OutputExport, th.current_export_id)
        state = export_state(s, out) if out is not None else None
    # Measured on the CURRENT version: a later file for the month is a new
    # bordereau whose exceptions stand on their own (even when there are more
    # than before), so "N of M resolved" is that file's M — the same total the
    # screens beside it show. The first version's count is only the fallback
    # before anything has been checked.
    total = state["total"] if state else base
    remaining = state["open"] if state else base
    blocking = state["blocking_open"] if state else 0
    return {"total": total, "fixed": max(0, total - remaining),
            "remaining": remaining, "blocking_remaining": blocking}


def progress_text(p: dict) -> Optional[str]:
    if not p["total"]:
        return None
    return f"{p['total']} exceptions → {p['fixed']} fixed → {p['remaining']} remaining"


def _names(s, th: Thread) -> dict:
    from db import Contract, Party, Program, Tenant
    prog = s.get(Program, th.program_id) if th.program_id else None
    ten = s.get(Tenant, th.tenant_id) if th.tenant_id else None
    broker = s.get(Party, th.broker_party_id) if th.broker_party_id else None
    con = (s.query(Contract.id, Contract.name, Contract.filename)
           .filter(Contract.id == th.contract_id).first() if th.contract_id else None)
    return {"programme": prog.name if prog else None,
            "contract": (con.name or con.filename) if con else None,
            "carrier": (ten.legal_name or ten.tenant_name) if ten else None,
            "broker": broker.legal_name if broker else None}


def status_json(s, th: Thread, include_exceptions: bool = False) -> dict:
    """THE status of a submission — what the API returns, the SFTP status file
    holds, the secure link and the carrier's panel show."""
    from db import OutputExport
    names = _names(s, th)
    cur = th.current
    out = s.get(OutputExport, cur.export_id) if cur.export_id else None
    state = export_state(s, out) if out is not None else None
    p = progress(s, th, state)
    delivered = next((v for v in reversed(th.versions)
                      if v.status in ("delivered", "delivered_flagged")), None)
    doc = {
        "reference": th.ref, "file": th.file_name, "version": cur.no,
        "status": th.status, "status_text": STATUS_WORDS.get(th.status, th.status),
        "message": th.note, "programme": names["programme"],
        "contract": names["contract"],
        "carrier": names["carrier"], "period": th.period,
        "progress": p, "progress_text": progress_text(p),
        "deadline": _iso(th.deadline_at), "delivered_at": _iso(th.delivered_at),
        "delivered_version": delivered.no if delivered else None,
        "versions": [{"version": v.no, "source": v.source, "is_file": v.is_file,
                      "status": v.status,
                      "status_text": STATUS_WORDS.get(v.status, v.status),
                      "message": v.note,
                      "exceptions": (_total(s.get(OutputExport, v.export_id))
                                     if v.export_id else None),
                      "open": state["open"] if (v is cur and state) else None,
                      "created_at": _iso(v.created_at)} for v in th.versions],
    }
    if include_exceptions:
        doc["exceptions"] = report_rows(s, out, cur.landing_id,
                                        rule=state["rule"] if state else None,
                                        decorated=state["exceptions"] if state else None) \
            if out is not None else []
    return doc


# ── the secure link: signed, nothing stored ─────────────────────────────────
#
# The link carries the reference, the recipient's email and an expiry, signed
# with the app's secret. Nothing about it is stored: it is valid while it is
# unexpired AND its submission is still waiting on the broker, so delivering
# the file closes every link at once. Codes and wrong tries are counted from
# activity_events, where every one is recorded anyway.

def _key() -> bytes:
    from settings import SETTINGS
    return hashlib.sha256(("fix-link:" + str(SETTINGS.JWT_SECRET)).encode()).digest()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s_: str) -> bytes:
    return base64.urlsafe_b64decode(s_ + "=" * (-len(s_) % 4))


def _sign(payload: dict) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest()[:18])
    return f"{body}.{sig}"


def _unsign(token: Optional[str], kind: str) -> Optional[dict]:
    if not token or len(token) > 600 or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    want = _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest()[:18])
    if not hmac.compare_digest(sig, want):
        return None
    try:
        doc = json.loads(_unb64(body))
    except Exception:  # noqa: BLE001
        return None
    if doc.get("k") != kind or int(doc.get("x") or 0) < time.time():
        return None
    return doc


def mint_link(th: Thread, email: str) -> str:
    return _sign({"k": "link", "r": th.ref, "e": email,
                  "x": int(time.time() + LINK_DAYS * 86400),
                  "n": secrets.token_hex(4)})


def read_link(token: str) -> Optional[dict]:
    """{'r': reference, 'e': email, …} for a genuine unexpired link."""
    return _unsign(token, "link")


def link_id(token: str) -> str:
    """A short, stable, non-secret name for one link (audit rows key on it)."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def code_for(token: str, window: int) -> str:
    mac = hmac.new(_key(), f"code:{token}:{window}".encode(), hashlib.sha256).hexdigest()
    return f"{int(mac, 16) % 1_000_000:06d}"


CODE_WINDOW_S = 300     # a code is good for its 5-minute window and the next


def check_code(token: str, code: str) -> bool:
    w = int(time.time() // CODE_WINDOW_S)
    code = (code or "").strip()
    return any(hmac.compare_digest(code, code_for(token, x)) for x in (w, w - 1))


def mint_session(token: str, minutes: int) -> str:
    return _sign({"k": "sess", "l": link_id(token), "x": int(time.time() + minutes * 60)})


def read_session(token: str, session: Optional[str]) -> bool:
    doc = _unsign(session, "sess")
    return bool(doc) and doc.get("l") == link_id(token)


def link_url(token: str) -> str:
    base = (os.getenv("APP_BASE_URL", "http://localhost:5173") or "").rstrip("/")
    return f"{base}/fix/{token}"


# ── messages: built in the transaction, sent after it commits ──────────────
#
# A message is queued on the SESSION and sent by a background thread once the
# session commits (so the broker never hears about something that was rolled
# back). Every outcome — sent, failed, skipped — is an activity_events row
# (bdx_notice_*), which is also how a message is never sent twice and what the
# carrier's panel reads. No retries: the carrier's "Send Again" is the retry.

def _already_sent(s, ref: str, key: str) -> bool:
    try:
        return s.execute(text(
            "SELECT 1 FROM activity_events WHERE target = :t "
            "AND action IN ('bdx_notice_sent', 'bdx_notice_skipped') "
            "AND CAST(details AS jsonb) ->> 'key' = :k LIMIT 1"),
            {"t": f"submission:{ref}", "k": key}).first() is not None
    except Exception:  # noqa: BLE001 — e.g. SQLite in tests: never block a send
        return False


def _queue(s, msg: dict) -> None:
    pend = s.info.setdefault("bdx_outbox", [])
    if any(m["key"] == msg["key"] for m in pend) or _already_sent(s, msg["ref"], msg["key"]):
        return
    pend.append(msg)
    if not s.info.get("bdx_hooked"):
        s.info["bdx_hooked"] = True
        event.listen(s, "after_commit", _after_commit)
        event.listen(s, "after_rollback", _after_rollback)


def _after_commit(session) -> None:
    msgs = session.info.pop("bdx_outbox", None)
    if msgs:
        threading.Thread(target=_send_all, args=(msgs,), name="bdx-notify",
                         daemon=True).start()


def _after_rollback(session) -> None:
    session.info.pop("bdx_outbox", None)


def _blocked_by_test_mode(recipient: Optional[str]) -> bool:
    try:
        from email_utils import _allowed_recipients
        allowed = _allowed_recipients()
    except Exception:  # noqa: BLE001
        return False
    return bool(allowed) and (recipient or "").strip().lower() not in allowed


def _send_one(m: dict) -> tuple[str, Optional[str]]:
    p = m["payload"]
    if m["channel"] == "sftp_file":
        write_sftp(p)
        return "sent", None
    if not notifications_enabled():
        return "skipped", "broker emails are switched off (BROKER_NOTIFY_ENABLED)"
    if _blocked_by_test_mode(m["recipient"]):
        return "skipped", "test mode: address not in MAIL_ALLOWED_RECIPIENTS"
    from email_utils import send_email
    att = p.get("attachment")
    files = [(att["name"], att["csv"].encode("utf-8"), "text/csv")] if att else []
    bdx = _highlighted_bdx((p.get("bdx") or {}).get("export_id"))
    if bdx:
        files.append(bdx)
    send_email(m["recipient"], p["subject"], p["html"], text=p.get("text"),
               account="NOTIFY", reply_to=p.get("reply_to"),
               attachments=files or None)
    return "sent", None


def _highlighted_bdx(export_id) -> Optional[tuple]:
    """(filename, bytes, media type) of the run's BDX through "Download BDX"'s
    own loader, or None — a missing file never stops the email."""
    if not export_id:
        return None
    try:
        from db import SessionLocal
        from main import export_file_bytes
        with SessionLocal() as s:
            return export_file_bytes(s, int(export_id))
    except Exception:  # noqa: BLE001
        log.warning("could not attach the BDX of export %s", export_id, exc_info=True)
        return None


def _send_all(msgs: list[dict]) -> None:
    from db import ActivityEvent, SessionLocal
    rows = []
    for m in msgs:
        try:
            status, err = _send_one(m)
        except Exception as exc:  # noqa: BLE001 — recorded; "Send Again" retries
            status, err = "failed", str(exc)[:500]
            log.info("notice %s to %s failed: %s", m["event"], m["recipient"], exc)
        rows.append(ActivityEvent(
            tenant_id=m["tenant_id"], actor="Kavachio",
            action=f"bdx_notice_{status}", target=f"submission:{m['ref']}",
            details={"key": m["key"], "event": m["event"], "channel": m["channel"],
                     "recipient": m["recipient"], "version": m["version"],
                     "error": err}))
    try:
        with SessionLocal() as s:
            s.add_all(rows)
            s.commit()
    except Exception:  # noqa: BLE001
        log.warning("could not record broker notices", exc_info=True)


def period_label(period: Optional[str]) -> Optional[str]:
    """'2026-07' → 'Jul 2026', '2026-Q1' → 'Q1 2026'; anything else as it is."""
    if not period:
        return None
    m = re.match(r"^(\d{4})-(\d{2})$", period)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), 1).strftime("%b %Y")
    m = re.match(r"^(\d{4})-(Q[1-4]|W\d{2})$", period, re.I)
    if m:
        return f"{m.group(2).upper()} {m.group(1)}"
    return period


def _queue_result_email(s, th: Thread, ver: Optional[Version], arrival, *,
                        event: str, suffix: str = "") -> None:
    if th is None:
        return
    ver = ver or th.current
    status = ver.status if event == "result" else (
        event if event in STATUS_WORDS else th.status)
    if event == "result" and status == "superseded":
        return
    # A clean or ready file is delivered in the same breath; the delivery
    # message says it better than a "result" message would.
    if event == "result" and status in ("clean", "ready", "delivered", "delivered_flagged"):
        event, status = "delivered", th.status
    if event == "delivered":
        status = th.status
    rcpts = recipients(s, th, arrival)
    if not rcpts:
        log.info("no one to tell about %s (%s)", th.ref, event)
        return
    names = _names(s, th)
    rows, csv_text, p = [], None, None
    from db import OutputExport
    out = s.get(OutputExport, th.current_export_id) if th.current_export_id else None
    if out is not None:
        state = export_state(s, out)
        p = progress(s, th, state)
        if status in ("with_exceptions", "ready", "delivered_flagged"):
            rows = report_rows(s, out, th.current.landing_id, rule=state["rule"],
                               decorated=state["exceptions"])
            open_rows = [r for r in rows if r["status"] == "open"]
            if open_rows:
                csv_text = report_csv(th.ref, ver.no, open_rows)
    for email in rcpts:
        token = mint_link(th, email) if status == "with_exceptions" else None
        payload = _email_payload(s, th, ver, event, status, names, rows, p,
                                 token=token, csv_text=csv_text,
                                 export_id=out.id if out is not None else None)
        _queue(s, {"key": f"v{ver.no}:{event}:{status}:email:{email.lower()}{suffix}",
                   "ref": th.ref, "tenant_id": th.tenant_id, "version": ver.no,
                   "event": event, "channel": "email", "recipient": email,
                   "payload": payload})


def _file_stem(name: Optional[str]) -> str:
    stem = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", name or "bordereau")
    return re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-") or "bordereau"


def _carrier_email(s, tenant_id) -> Optional[str]:
    """The carrier's best contact address — the one a broker can write to."""
    try:
        from submission_calendar_service import carrier_contacts
        cs = carrier_contacts(s, [tenant_id]).get(tenant_id, [])
        return cs[0]["email"] if cs and cs[0].get("email") else None
    except Exception:  # noqa: BLE001 — a missing address never stops the email
        return None


def _email_payload(s, th, ver, event, status, names, rows, p, *, token, csv_text,
                   export_id=None):
    """The broker's email. Says WHICH file in the words the portal uses — its
    name, programme and period — never the internal reference: a resend for
    the same programme and period is matched to this submission by itself."""
    from html import escape
    from notifications import notification_email_html, _email_text
    open_rows = [r for r in rows if r["status"] == "open"]
    n_open = len(open_rows)
    blocking = sum(1 for r in open_rows if r["holds_file"])
    file_name = th.file_name
    carrier = names["carrier"] or "the carrier"
    period = period_label(th.period)
    exc = lambda n: f"{n} exception{'s' if n != 1 else ''}"   # noqa: E731

    note = ver.note or th.note
    if status == "with_exceptions":
        title = f"Action required: {exc(n_open)} to resolve"
        first = (f"Your bordereau has {exc(n_open)} that must be resolved before it is "
                 f"delivered to {carrier}." if blocking == n_open else
                 f"Your bordereau has {exc(n_open)}; {blocking} must be resolved before it "
                 f"is delivered to {carrier}.")
        body = [first, "Resolve them online using the secure link below, or send a "
                       "corrected file through your usual channel."]
    elif status == "failed":
        title = "Action required: file could not be processed"
        body = [note or "The file could not be read.",
                "Please correct it and send it again through your usual channel."]
    elif status == "rejected":
        title = "File rejected"
        body = [note or "The file could not be accepted.", "Please send a corrected file."]
    elif status == "on_hold":
        title = "File on hold — no action needed"
        body = ["We are resolving an issue on our side. No action is needed from you."]
    elif status == "duplicate":
        title = "File already received"
        # A duplicate is held, not thrown away: the carrier can release it from
        # Files Received. Say so, or the broker thinks it is a dead end.
        reach = _carrier_email(s, th.tenant_id)
        body = ["This file is identical to one already received, so it was not processed again.",
                f"If you want this file processed, please contact {carrier}"
                + (f" at {reach}." if reach else ".")]
    elif event == "deadline_hold":
        title = "Deadline passed — file on hold"
        body = [f"The deadline passed with exceptions still open. {carrier} will decide "
                "whether to accept the file as it is."]
    elif status == "delivered_flagged":
        title = "Delivered with open exceptions"
        body = [f"Your bordereau was delivered to {carrier} with {exc(n_open)} still open."]
    else:
        title = "Delivered"
        body = [f"Your bordereau has been delivered to {carrier}. No action is needed."]
    subject = f"{title} · {file_name}"

    due = (_aware(th.deadline_at).strftime("%d %b %Y")
           if status == "with_exceptions" and th.deadline_at else None)
    facts = [("File", file_name), ("Programme", names["programme"]),
             ("Reporting period", period),
             ("Version", str(ver.no) if len(th.versions) > 1 else None),
             ("Due by", due)]

    # The first few, in the broker's own terms; the rest are in the attachment.
    table = ""
    if open_rows:
        td = "padding:6px 8px;border-bottom:1px solid #eef0f4;font-size:12px;color:#1f2937"
        cells = "".join(
            "<tr>" + "".join(f"<td style='{td}'>{escape(str(v if v is not None else ''))}</td>"
                             for v in (r["sheet"], _shown_row(r), r["column"], _shown_value(r),
                                       r["expected_value"]))
            + "</tr>" for r in open_rows[:5])
        th_ = "text-align:left;padding:6px 8px;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.3px"
        head = "".join(f"<th style='{th_}'>{h}</th>"
                       for h in ("Sheet", "Row", "Column", "Current", "Expected"))
        more = (f"<p style='font-size:12px;color:#6b7280;margin:8px 0 0'>+ {n_open - 5} more in the "
                f"attached list — the attached bordereau highlights every one.</p>"
                if n_open > 5 else
                "<p style='font-size:12px;color:#6b7280;margin:8px 0 0'>The attached bordereau "
                "highlights every exception.</p>")
        table = (f"<p style='font-size:13px;font-weight:600;color:#111827;margin:0 0 6px'>"
                 f"First exceptions</p><table style='border-collapse:collapse;width:100%'>"
                 f"<tr>{head}</tr>{cells}</table>{more}")
    link = link_url(token) if token else None
    html = notification_email_html(
        title, "\n\n".join(body), facts, link=link,
        link_label="Review exceptions", action=None,
        footer=(f"This secure link is for your use only and expires in {LINK_DAYS} days."
                if link else "Sent by Kavachio Bordereau Management."))
    if table:
        html = html.replace("</body>", f"<div style='max-width:560px;margin:0 auto;"
                                       f"padding:0 24px 24px'>{table}</div></body>", 1)
    txt = _email_text(title, "\n\n".join(body), facts, link)
    att = None
    if csv_text and status in ("with_exceptions", "delivered_flagged"):
        att = {"name": f"{_file_stem(file_name)}-exceptions.csv", "csv": csv_text}
    # The bordereau itself with its exceptions highlighted — the same file
    # "Download BDX" gives. Only its id is queued; the bytes are read at send.
    bdx = ({"export_id": export_id} if export_id and open_rows
           and status in ("with_exceptions", "delivered_flagged") else None)
    reply_to = None
    if th.channel == "email" and th.route_id:
        try:
            from intake_routes import _send_to
            route = s.get(IntakeRoute, th.route_id)
            reply_to = _send_to(route) if route is not None else None
        except Exception:  # noqa: BLE001 — a reply address is a convenience
            reply_to = None
    return {"subject": subject, "html": html, "text": txt, "attachment": att,
            "bdx": bdx, "reply_to": reply_to}


def _alert_carrier(s, th, ver, why, event: str = "on_hold") -> None:
    """Something on OUR side (or the carrier's) needs a person."""
    from audit import log_activity
    try:
        log_activity(th.tenant_id, "system", f"bdx_submission_{event}",
                     target=f"submission:{th.ref}",
                     details={"reference": th.ref, "file": th.file_name,
                              "version": ver.no if ver else th.current.no,
                              "reason": (why or "")[:500]})
    except Exception:  # noqa: BLE001
        pass
    if event != "deadline_hold":
        return
    from notifications import _app_link, _email_text, notification_email_html
    from submission_calendar_service import carrier_contacts
    names = _names(s, th)
    title = "Decision needed: file on hold"
    body = (f"The deadline passed with exceptions still open. Accept the file as it is "
            f"in Files Received, or wait for {names['broker'] or 'the broker'}.")
    facts = [("File", th.file_name), ("Broker", names["broker"]),
             ("Programme", names["programme"]), ("Reporting period", period_label(th.period)),
             ("Open exceptions", str(progress(s, th)["remaining"]))]
    link = _app_link("/files")
    for c in carrier_contacts(s, [th.tenant_id]).get(th.tenant_id, []):
        _queue(s, {"key": f"carrier_deadline:{c['email'].lower()}", "ref": th.ref,
                   "tenant_id": th.tenant_id, "version": th.current.no,
                   "event": "carrier_deadline", "channel": "email",
                   "recipient": c["email"],
                   "payload": {"subject": f"{title} · {th.file_name}",
                               "html": notification_email_html(
                                   title, body, facts, link=link,
                                   link_label="Open Files Received"),
                               "text": _email_text(title, body, facts, link)}})


def notices(s, ref: str) -> list[dict]:
    """Every message about this submission, newest first (the carrier's panel)."""
    from db import ActivityEvent
    rows = (s.query(ActivityEvent)
            .filter(ActivityEvent.target == f"submission:{ref}",
                    ActivityEvent.action.in_(("bdx_notice_sent", "bdx_notice_failed",
                                              "bdx_notice_skipped")))
            .order_by(ActivityEvent.id.desc()).limit(50).all())
    out = []
    for r in rows:
        d = r.details or {}
        out.append({"event": d.get("event"), "channel": d.get("channel"),
                    "recipient": d.get("recipient"), "version": d.get("version"),
                    "status": r.action.rsplit("_", 1)[-1], "error": d.get("error"),
                    "at": _iso(r.created_at)})
    return out


# ── SFTP: write the answer next to the folder they dropped the file in ─────

def _receipt_doc(th, ver, arrival) -> dict:
    return {"reference": th.ref, "version": ver.no,
            "file": arrival.filename, "received_at": _iso(arrival.received_at),
            "status": ver.status, "status_text": STATUS_WORDS.get(ver.status, ver.status),
            "message": ver.note, "period": th.period}


def _queue_sftp(s, th, ver, route, kind: str, doc: dict) -> None:
    ver_no = ver.no if ver is not None else th.current.no
    _queue(s, {"key": f"v{ver_no}:{kind}:{doc.get('status')}:sftp", "ref": th.ref,
               "tenant_id": th.tenant_id, "version": ver_no, "event": kind,
               "channel": "sftp_file", "recipient": route.address,
               "payload": {"route_address": route.address, "kind": kind, "doc": doc,
                           "reference": th.ref, "version": ver_no}})


def _queue_sftp_for(s, th, ver, kind: str) -> None:
    if th.channel != "sftp" or not th.route_id:
        return
    route = s.get(IntakeRoute, th.route_id)
    if route is None:
        return
    _queue_sftp(s, th, ver, route, kind, status_json(s, th, include_exceptions=True))


def write_sftp(payload: dict) -> None:
    """Write receipt / status files into the broker's own `outbound/` folder."""
    import intake_service as svc
    base = svc.sftp_root() / payload["route_address"] / "outbound"
    base.mkdir(parents=True, exist_ok=True)
    ref, ver, kind = payload["reference"], payload.get("version"), payload["kind"]
    doc = payload["doc"]
    stem = f"{ref}-v{ver}" if ver else ref

    def _atomic(name: str, data: str) -> None:
        tmp = base / f".{name}.tmp"
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, base / name)

    _atomic(f"{stem}.{kind}.json", json.dumps(doc, indent=2, default=str))
    if kind == "result":
        _atomic(f"{ref}.status.json", json.dumps(
            {k: v for k, v in doc.items() if k != "exceptions"}, indent=2, default=str))
        excs = [r for r in (doc.get("exceptions") or []) if r.get("status") == "open"]
        if excs:
            _atomic(f"{stem}.exceptions.csv", report_csv(ref, ver, excs))


# ── the secure link's Validate: the next version, checked, not kept ─────────

def _correction_inputs(s, th: Thread) -> dict:
    """What a render of this submission's corrections needs."""
    from db import OutputExport
    if th.current_export_id is None:
        raise ValueError("nothing to correct")
    if th.status not in OPEN_STATUSES:
        raise ValueError("this submission is not waiting for answers")
    old = s.get(OutputExport, th.current_export_id)
    landing_id = th.current.landing_id or s.execute(text(
        "SELECT id FROM landing_record WHERE output_export_id = :e "
        "ORDER BY id DESC LIMIT 1"), {"e": old.id}).scalar()
    if landing_id is None:
        raise ValueError("this file cannot be corrected here — send a corrected file")
    return {"landing_id": int(landing_id), "contract_id": old.contract_id,
            "pipeline_id": old.pipeline_id, "period": th.period,
            "scope": {"carrier_party_id": old.carrier_party_id,
                      "program_id": old.program_id,
                      "broker_party_id": old.broker_party_id,
                      "contract_id": old.contract_id}}


async def validate_corrections(ref: str, actor: str) -> dict:
    """Run every rule over the file WITH the broker's saved corrections, as the
    render's own pre-submission self-check (check_only): a throwaway output that
    ticks no calendar period, sends nothing, loads nothing and is linked to no
    file. Says which corrected values still break a rule and what the next
    version would leave open."""
    from db import OutputExport, SessionLocal
    import direct_routes as dr
    with SessionLocal() as s:
        th = load(s, ref)
        if th is None:
            raise ValueError("nothing to correct")
        inp = _correction_inputs(s, th)
    result = await dr._render_landing(
        inp["landing_id"], inp["contract_id"], None, actor, {}, auto_ingest=False,
        reuse_export_id=None, rule_scope_pipeline_id=inp["pipeline_id"],
        scope=inp["scope"], check_only=True, mark_calendar=False)
    check_id = result["export_id"]
    with SessionLocal() as s:
        th = load(s, ref)
        out = s.get(OutputExport, check_id)
        decisions = {(r["output_sheet"], r["output_row"], r["output_field"]): r["kind"]
                     for r in s.execute(text(
                         "SELECT output_sheet, output_row, output_field, kind "
                         "FROM landing_correction WHERE landing_id = :l"),
                         {"l": inp["landing_id"]}).mappings()}
        rule = rule_for(s, th.program_id)
        rows = report_rows(s, out, inp["landing_id"], rule=rule)

        def _kind(r):
            try:
                return decisions.get((r["output_sheet"], int(r["output_row"]), r["output_field"]))
            except (TypeError, ValueError):
                return None
        failing = [r for r in rows if _kind(r) == "fix"]
        unanswered = [r for r in rows if _kind(r) is None]
        fixes = sum(1 for k in decisions.values() if k == "fix")
        doc = {
            "version": th.current.no + 1,
            "corrected": fixes,
            "corrected_ok": max(0, fixes - len(failing)),
            "still_failing": failing[:200],
            "still_failing_count": len(failing),
            "open_after": len(failing) + len(unanswered),
            "blocking_after": sum(1 for r in failing + unanswered if r["holds_file"]),
        }
        from db import ActivityEvent
        s.add(ActivityEvent(
            tenant_id=th.tenant_id, actor=actor, action="bdx_fix_link_validated",
            target=f"submission:{ref}",
            details={"reference": ref, "check_export_id": check_id,
                     **{k: v for k, v in doc.items() if k != "still_failing"}},
            actor_broker_party_id=th.broker_party_id))
        s.commit()
        return doc


# ── the secure link's Submit: the corrected data, sent like a file ──────────
#
# Submit is the broker SENDING the corrected data — the same as uploading a
# corrected file by API, without a file to upload. The broker's own data (the
# landing of the version they corrected) with their value corrections applied
# — the render's own overlay, _apply_landing_corrections — becomes a workbook
# that lands through intake_service exactly as an API file does: the intake
# checks, a kept copy, a row in Files Received, the auto-run, the result email
# and the programme's delivery rule. It is the next version of the submission
# (it quotes the reference), and the version before it is untouched.
#
# Decisions that are not a value in the data — Approve / Dismiss, and fixes to
# a computed column — are carried onto the new file's landing just before it
# runs (carry_decisions, called by intake_autorun), so they are not asked again.

_ISO_DT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$")


def _landing_workbook(data: dict) -> bytes:
    """The broker's data as a workbook, sheet by sheet, columns in their order."""
    import pandas as pd
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        for name, sh in (data.get("sheets") or {}).items():
            cols = list(sh.get("columns") or [])
            rows = [{k: (datetime.fromisoformat(v) if isinstance(v, str) and _ISO_DT.match(v) else v)
                     for k, v in (r or {}).items()} for r in (sh.get("rows") or [])]
            pd.DataFrame(rows, columns=cols or None).to_excel(
                w, sheet_name=(str(name)[:31] or "Sheet1"), index=False)
    return buf.getvalue()


def submit_corrections(ref: str, email: str) -> dict:
    """Send the corrected data as the next version. Returns where it stands
    (processing — the auto-run picks it up like any arrival)."""
    from db import LandingRecord, SessionLocal
    import direct_routes as dr
    import intake_service as isvc
    with SessionLocal() as s:
        th = load(s, ref)
        if th is None:
            raise ValueError("nothing to correct")
        inp = _correction_inputs(s, th)
        rec = s.get(LandingRecord, inp["landing_id"])
        data = dr._apply_landing_corrections(s, inp["landing_id"], rec.data or {})
        body = _landing_workbook(data)
        # The same name as the file it corrects: it is the next VERSION of
        # that file, and every screen numbers versions itself — a "(v4)" in
        # the name made it read as a different file.
        corrected = th.current.arrival.filename if th.current.is_file else th.file_name
        fname = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", corrected or "bordereau") + ".xlsx"
        blob_ref = None
        try:
            import storage
            blob_ref, _ = storage.store_or_keep("intake", th.tenant_id, fname, body)
        except Exception:  # noqa: BLE001 — land_file keeps a database copy instead
            log.warning("storage failed for the corrected data of %s", ref, exc_info=True)
        if th.route is not None:
            arrival = isvc.land_file(
                s, tenant_id=th.tenant_id, filename=fname, file_bytes=body,
                route=th.route, claimed_sender=email, blob_ref=blob_ref,
                period=th.period, replaces=ref,
                program_id=th.program_id, contract_id=th.contract_id)
        else:
            # A file first sent on Process Bordereau has no channel: it lands the
            # way that page lands one, and the auto-run takes it from there.
            arrival = isvc.land_manual_upload(
                s, tenant_id=th.tenant_id, filename=fname, file_bytes=body,
                user_id=None, broker_party_id=th.broker_party_id,
                program_id=th.program_id, confirm_duplicate=True, blob_ref=blob_ref,
                period=th.period, replaces=ref, contract_id=th.contract_id)
            if arrival.outcome == "accepted":
                arrival.run_state = None
            arrival.claimed_sender = email
        arrival.matched_by = "secure_link"
        s.commit()
        aid = arrival.id
        if arrival.outcome != "accepted":
            raise ValueError(arrival.turned_away_reason or "The corrected data could not be accepted.")
    try:
        import intake_autorun
        intake_autorun.wake()
    except Exception:  # noqa: BLE001 — the loop finds it on its next tick anyway
        pass
    with SessionLocal() as s:
        doc = status_json(s, load(s, ref))
        doc["arrival_id"] = aid
        return doc


def carry_decisions(arrival_id: int, landing_id: int) -> None:
    """intake_autorun, between landing a file and checking it: a file sent from
    the secure link takes the broker's earlier decisions that are not values in
    its data. Every other file: nothing. Never raises."""
    from db import SessionLocal
    try:
        with SessionLocal() as s:
            a = s.get(FileArrival, arrival_id)
            if a is None or a.matched_by != "secure_link" or not a.submission_ref:
                return
            th = load(s, a.submission_ref)
            prev = next((v.landing_id for v in reversed(th.versions)
                         if v.no < (a.version_no or 0) and v.landing_id), None) if th else None
            if not prev or prev == landing_id:
                return
            n = s.execute(text(
                "INSERT INTO landing_correction (tenant_id, landing_id, output_sheet, "
                " output_row, output_field, rule_id, policy_number, kind, reason, "
                " input_sheet, input_row_index, source_column, old_value, new_value, "
                " decided_by, decided_at) "
                "SELECT tenant_id, :new, output_sheet, output_row, output_field, rule_id, "
                " policy_number, kind, reason, NULL, NULL, NULL, old_value, new_value, "
                " decided_by, decided_at FROM landing_correction "
                "WHERE landing_id = :old AND ("
                "  (kind IN ('approve', 'dismiss') AND new_value IS NULL) OR "
                "  (kind IN ('fix', 'approve') AND input_sheet IS NULL AND new_value IS NOT NULL)) "
                "ON CONFLICT (landing_id, output_sheet, output_row, output_field) DO NOTHING"),
                {"new": landing_id, "old": prev}).rowcount
            s.commit()
            log.info("carried %s decision(s) from landing %s to %s for %s",
                     n, prev, landing_id, a.submission_ref)
    except Exception:  # noqa: BLE001
        log.warning("could not carry decisions onto landing %s", landing_id, exc_info=True)
