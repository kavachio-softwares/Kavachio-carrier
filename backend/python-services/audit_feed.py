"""The Audit Logs feed — one trail, out of three stores, scoped to the viewer.

WHAT IT READS
─────────────
Three durable audit stores already exist (audit.py writes them all):

  activity_events        what someone DID — every mutation, plus the named
                         business events (a file run, a setup activated)
  auth_audit             who signed in, out, failed, or reset a password
  access_log             who downloaded or opened output / source data
  exception_decision_log who resolved which exception, on which file

The fourth one REPLACES activity_events' `exception_decided` rows rather than
joining them. They are the same five events in this database, but the activity
row records only `broker:<party id>` as the actor and knows nothing else, while
the decision log names the person, their role, their broker, the file and every
cell they touched — it is written inside the decision's own transaction, so it
cannot disagree with the decision either. Reading both would list each
resolution twice, once anonymously. See _rows().

They are kept apart on purpose (different volume, different retention), and
this module is the one place that reads them back as a single feed: one row
shape, newest first, with the same filters applied to each.

WHO SEES WHAT
─────────────
Scoping is by SEAT, not by tenant, because a broker seat has no tenant at all
(their token carries a broker party instead). `scope_for` turns the viewer into
the exact set of seats whose rows they may read:

  broker user     their own trail, nothing else
  broker admin    every seat in their broker organisation, their own included
  carrier user    their own trail, plus every broker seat the carrier works with
  carrier admin   every seat at the carrier, plus every broker seat in reach
  Kavachio admin  everything, every carrier and every broker

NAMING THE ACTOR
────────────────
Every row names WHO did it, as a person wherever the row allows (6 Oct 2026):

  a person             their name, and "Broker · <company>" / "Carrier" — the
                       carrier invited each broker person by name and needs to
                       know which one acted. (A retired broker-user seat still
                       reads as its company to a carrier.)
  the review link      the person the "Exceptions to resolve" email went to:
                       the link has no login, so its rows carry the address
  Kavachio itself      "Kavachio · Automatic" — the auto-run, the status
                       emails, the calendar reminders, the bordereau sent on to
                       the carrier. Never an anonymous "System".
  unknown              a broker row written before the person was recorded
                       says "person not recorded" rather than guess.

Naming happens on the way OUT, so the stored rows are never rewritten.

HISTORY
───────
`activity_events.actor_user_id` was added with the Audit Logs screen, so rows
written before it are resolved at read time from the `actor` string: an email
finds the person, and `broker:<party id>` finds the company (but not which of
its people acted — that row simply never recorded it).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import and_, case, cast, desc, func, or_
from sqlalchemy.dialects.postgresql import JSONB

import audit_names
from auth_deps import BROKER_ROLES, Principal, normalize_role
from db import (
    AccessLog, ActivityEvent, AppUser, AuthAudit, ExceptionDecisionLog, Party,
    Program, ProgramBroker, SessionLocal, Tenant,
)

# Categories, in the order the filter offers them.
CATEGORIES = ("activity", "auth", "access", "decision")

# How deep the merged feed may be paged. Three stores are paged independently
# and merged in memory, so each one has to be read to offset+size — a bound is
# what keeps a hand-typed ?page=900000 from asking for 3 x 18M rows.
MAX_OFFSET = 20_000
MAX_PAGE_SIZE = 200
MAX_EXPORT_ROWS = 20_000


# ---------------------------------------------------------------------------
# the viewer
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Viewer:
    """The signed-in seat, as the feed needs to know it."""
    principal: Principal
    seat: str                      # platform | carrier_admin | carrier_user | broker_admin | broker_user
    tenant_id: Optional[int]
    broker_party_id: Optional[int]

    @property
    def is_platform(self) -> bool:
        return self.seat == "platform"

    @property
    def is_carrier(self) -> bool:
        return self.seat in ("carrier_admin", "carrier_user")

    @property
    def is_broker(self) -> bool:
        return self.seat in ("broker_admin", "broker_user")


def viewer_for(s, p: Principal) -> Viewer:
    """Resolve the seat. Both carrier seats hold the same `carrier_admin` DB
    role — the organisation's owner pointer is what tells the admin from a user
    (the same rule hooks/useCarrierSeat applies in the UI). An organisation with
    no owner recorded is read as the admin's, so a legacy carrier is not locked
    out of its own trail."""
    role = normalize_role(p.role)
    if role == "kavachio_admin":
        return Viewer(p, "platform", None, None)
    if role in BROKER_ROLES:
        u = s.query(AppUser).filter(AppUser.id == p.user_id).first()
        bid = int(u.broker_party_id) if u and u.broker_party_id else None
        return Viewer(p, "broker_admin" if role == "broker_admin" else "broker_user",
                      None, bid)
    tid = p.tenant_id
    owner = None
    if tid is not None:
        t = s.get(Tenant, tid)
        owner = getattr(t, "owner_user_id", None)
    is_admin = owner is None or owner == p.user_id
    return Viewer(p, "carrier_admin" if is_admin else "carrier_user", tid, None)


# ---------------------------------------------------------------------------
# the scope
# ---------------------------------------------------------------------------

@dataclass
class Scope:
    """Exactly whose rows this viewer may read."""
    everything: bool = False
    user_ids: set[int] = field(default_factory=set)
    emails: set[str] = field(default_factory=set)      # for rows predating actor_user_id
    broker_party_ids: set[int] = field(default_factory=set)
    tenant_ids: set[int] = field(default_factory=set)

    @property
    def broker_actor_keys(self) -> set[str]:
        """The `broker:<party id>` display strings audit.actor_for writes."""
        return {f"broker:{b}" for b in self.broker_party_ids}


def brokers_of_carrier(s, tenant_id: int) -> set[int]:
    """Every broker organisation put on one of this carrier's programmes.

    Joined through the programme, whose carrier is always set — program_broker
    has a tenant_id of its own but legacy rows may lack it, and a broker missing
    from this set would silently vanish from the carrier's audit trail.
    Inactive pairs are INCLUDED: a broker taken off a programme last month still
    did what it did, and an audit trail that forgets is not one.
    """
    rows = (s.query(ProgramBroker.broker_party_id)
              .join(Program, Program.id == ProgramBroker.program_id)
              .filter(Program.tenant_id == tenant_id)
              .distinct().all())
    return {int(r[0]) for r in rows if r[0] is not None}


def _seats(s, *, tenant_id: int | None = None,
           broker_party_ids: Iterable[int] | None = None) -> tuple[set[int], set[str]]:
    """(user ids, emails) of the seats at a carrier, or in a set of brokers."""
    q = s.query(AppUser.id, AppUser.email)
    if tenant_id is not None:
        q = q.filter(AppUser.tenant_id == tenant_id)
    else:
        ids = list(broker_party_ids or ())
        if not ids:
            return set(), set()
        q = q.filter(AppUser.broker_party_id.in_(ids))
    ids_out: set[int] = set()
    emails_out: set[str] = set()
    for uid, email in q.all():
        ids_out.add(int(uid))
        if email:
            emails_out.add(email)
    return ids_out, emails_out


def scope_for(s, v: Viewer) -> Scope:
    """The seats `v` may read. See WHO SEES WHAT at the top of this file."""
    if v.is_platform:
        return Scope(everything=True)

    me = s.query(AppUser).filter(AppUser.id == v.principal.user_id).first()
    my_email = {me.email} if me and me.email else set()

    if v.seat == "broker_user":
        # A seat inside the broker, accountable for its own work only.
        return Scope(user_ids={v.principal.user_id}, emails=my_email)

    if v.seat == "broker_admin":
        if v.broker_party_id is None:
            return Scope(user_ids={v.principal.user_id}, emails=my_email)
        uids, emails = _seats(s, broker_party_ids=[v.broker_party_id])
        uids.add(v.principal.user_id)
        return Scope(user_ids=uids, emails=emails | my_email,
                     broker_party_ids={v.broker_party_id})

    # --- a carrier seat ----------------------------------------------------
    brokers = brokers_of_carrier(s, v.tenant_id) if v.tenant_id is not None else set()
    b_uids, b_emails = _seats(s, broker_party_ids=brokers)

    if v.seat == "carrier_user":
        # Their own trail, and everything their brokers did. Not their
        # colleagues' — that is the carrier admin's view.
        return Scope(user_ids={v.principal.user_id} | b_uids,
                     emails=my_email | b_emails,
                     broker_party_ids=brokers)

    c_uids, c_emails = _seats(s, tenant_id=v.tenant_id)
    return Scope(user_ids=c_uids | b_uids | {v.principal.user_id},
                 emails=c_emails | b_emails | my_email,
                 broker_party_ids=brokers,
                 tenant_ids={v.tenant_id} if v.tenant_id is not None else set())


# ---------------------------------------------------------------------------
# the words
# ---------------------------------------------------------------------------

ROLE_WORDS = {
    "kavachio_admin": "Kavachio Admin",
    "carrier_admin":  "Carrier",
    "carrier_user":   "Carrier User",
    "broker_admin":   "Broker",
    "operator":       "Broker User",
}

# Business actions, in the words the screen shows. Anything missing falls back
# to _humanize(), so a new event reads sensibly the day it is first written
# rather than waiting for this table to catch up.
ACTION_WORDS = {
    # auth
    "login_success":   "Signed in",
    "login_failed":    "Sign-in failed",
    "logout":          "Signed out",
    "forgot_request":  "Requested a password reset",
    "password_reset":  "Reset their password",
    "password_changed": "Changed their password",
    "token_refresh":   "Renewed their session",
    # files + runs
    "bdx_uploaded":              "Sent in a bordereau file",
    "direct_setup_uploaded":     "Uploaded a sample bordereau for a Bordereau Setup",
    "supplement_uploaded":       "Uploaded a supporting file",
    "contract_uploaded":         "Uploaded a contract",
    "output_generated":          "Process Bordereau: Checked a bordereau and built the output BDX",
    "direct_output_generated":   "Process Bordereau: Checked a bordereau and built the output BDX",
    "direct_output_checked":     "Test-checked a file (nothing was sent)",
    "datamodel.ingest":          "Loaded the file's rows into the data model",
    "validation.run":            "Ran the rule checks on a file",
    # exceptions
    "exception_decided":         "Resolved exceptions",
    # setup
    "bordereau_setup_completed": "Completed a Bordereau Setup",
    "bordereau_setup_activated": "Activated a Bordereau Setup",
    "bordereau_setup_needed": "A bordereau arrived before its programme had a live Bordereau Setup",
    # Approval: a carrier user builds a setup, the carrier admin decides.
    # Worded from the actor's side, because that is who the row is about.
    "bordereau_setup_submitted": "Sent a Bordereau Setup for approval",
    "bordereau_setup_approved":  "Approved a Bordereau Setup",
    "bordereau_setup_rejected":  "Sent a Bordereau Setup back",
    "bdx_setup_updated":         "Updated a Bordereau Setup",
    "bdx_setup_deleted":         "Deleted a Bordereau Setup",
    "sheet_bindings_saved":      "Saved the sheet bindings",
    "input_mapper_generated":    "Generated the column mapping",
    "input_mapper_updated":      "Updated the column mapping",
    "input_mapper_activated":    "Activated the column mapping",
    "output_template_generated": "Created an output template",
    "output_template_updated":   "Updated an output template",
    "output_template_activated": "Activated an output template",
    "output_template_refreshed": "Refreshed an output template",
    "canonical_fields_saved":    "Saved the data-model fields",
    "datamodel_mapping_proposed": "Proposed a data-model mapping",
    "mapping_task_resolved":     "Resolved a data-mapping task",
    # the book
    "tenant_created":   "Created a carrier",
    "tenant_updated":   "Updated the carrier",
    "party_created":    "Added a party",
    "party_updated":    "Updated a party",
    "party_contact_added":   "Added a party contact",
    "party_contact_removed": "Removed a party contact",
    "program_created":  "Created a programme",
    "program_updated":  "Updated a programme",
    "submission_schedule_updated": "Updated a submission schedule",
    # contracts + rules
    "contract.rules_generated":  "Built the checks from a contract's terms",
    "contract.rules_built":      "Built the contract rules",
    "clause.resolved_to_field":  "Mapped a clause to a field",
    "rule_created":     "Created a rule",
    "rule_updated":     "Updated a rule",
    "rule_deleted":     "Deleted a rule",
    "rule.disabled":    "Disabled a rule",
    "rule.tolerance_changed":      "Changed a rule tolerance",
    "rule.output_field_changed":   "Changed a rule's output field",
    "rule.variation_value_added":  "Accepted a spelling on a rule",
    "rule.variation_value_removed": "Removed a spelling from a rule",
    # people
    "user_invited":     "Invited a user",
    "invite_resent":    "Resent an invitation",
    "user_updated":     "Updated a user",
    "user_deleted":     "Removed a user",
    "profile_updated":  "Updated their profile",
    "ownership_transferred": "Transferred ownership",
    "extra_field_saved":   "Saved a custom field",
    "extra_field_adopted": "Adopted a custom field",
    # the contract's life
    "contract_raised":            "Created a new contract",
    "contract_edited":            "Edited a contract",
    "contract_submitted":         "Submitted a contract",
    "contract_sent_for_review":   "Sent a contract for review",
    "contract_sent_back":         "Sent a contract back to be changed",
    "contract_awaiting_review":   "Sent a contract up to the carrier",
    # The gate that matters: the broker has agreed, and the contract is now on
    # the carrier admin's desk for signature and nobody else's.
    "contract_awaiting_signature": "Broker agreed the terms — waiting on the "
                                   "carrier to sign",
    "contract_accepted":          "Accepted an uploaded contract",
    "contract_pushed_back":       "Broker asked the carrier for changes to the contract",
    "contract_signed_off":        "Signed the contract for the carrier",
    "contract_terms_accepted":    "Accepted the contract terms",
    "contract_changes_requested": "Requested changes to a contract",
    "contract_approved":          "Approved a contract",
    "contract_rejected":          "Rejected a contract",
    "contract_activated":         "Activated a contract",
    "contract_terminated":        "Terminated a contract",
    "contract_renewed":           "Renewed a contract",
    "contract_signed":            "Signed a contract",
    "contract_signed_copy_submitted": "Submitted the signed contract",
    "contract_document_attached": "Attached a document to a contract",
    "contract_wording_saved":     "Saved the contract wording",
    "contract_wording_drafted":   "Drafted the contract wording",
    # POST /esign/contracts/{id}/signing-session: the person opening the
    # contract to sign it (for the carrier, this also starts the signing round).
    # A broker's copy is never shown — their signature follows as its own row.
    "signature_round_started":    "Opened the contract to sign it",
    "signature_envelope_created": "Prepared a signing envelope",
    "signature_envelope_sent":    "Sent a signing envelope",
    "signing_code_resent":        "Resent their signing code",
    "contract_signature_declined": "Declined to sign a contract",
    "signature_reminder_sent":    "Reminded a signer",
    "signature_round_voided":     "Voided a signing round",
    "contract_endorsed":          "Endorsed a contract",
    "contract_checks_bound":      "Linked the contract's terms to the bordereau checks",
    "contract_mapped_to_template": "Mapped a contract to a template",
    "contract_review_skipped":    "Skipped the contract review",
    "contract_signed_from_email": "Signed the contract (e-signature)",
    "signing_code_verified":      "Entered their signing code to sign a contract",
    # the broker mesh
    "broker_added":               "Added a broker",
    "broker_linked":              "Linked an existing broker",
    "broker_invitation_declined": "Declined their invitation",
    "broker_invitation_withdrawn": "Withdrew an invitation",
    "broker_removed_from_programme": "Removed a broker from a programme",
    "broker_user_created":        "Created a broker user",
    "broker_invitation_accepted": "Accepted their invitation",
    "broker_put_on_programme":    "Added a broker to a programme",
    # The approval that now stands in front of both of those. A carrier user
    # asks; the carrier admin answers. Named for what HAPPENED, so a reader
    # scanning the log is never told a broker was added on a day one was only
    # asked about.
    "broker_request_submitted":   "Asked to bring a broker on board",
    "broker_request_approved":    "Approved bringing a broker on board",
    "broker_request_rejected":    "Turned down a broker",
    "broker_request_withdrawn":   "Withdrew their broker request",
    # the run
    # The person's click; the result follows as its own row ("Checked a
    # bordereau and built the output BDX", with the row and exception counts).
    "bordereau_run":              "Uploaded a bordereau in the portal and ran the checks",
    "bordereau_setup_created":    "Created a Bordereau Setup",
    "missing_columns_analyzed":   "Checked which columns a file is missing",
    "output_template_fields_saved": "Saved the output template fields",
    "output_template_from_standard": "Created a template from a standard",
    "output_sources_analyzed":    "Analysed the source files",
    "intake_route_created":       "Created an ingestion channel",
    "intake_route_updated":       "Changed an ingestion channel",
    "intake_route_contacts_updated": "Changed who hears about a channel's files",
    "intake_key_created":         "Issued a channel API key",
    "intake_key_revoked":         "Revoked a channel API key",
    # The same button collects from a mailbox, a folder or an SFTP server.
    "mailbox_polled":             "Checked a channel for new files",
    "intake_sftp_tested":         "Tested an SFTP server connection",
    "intake_guide_sent":          "Asked Kavachio to email a broker how to send files",
    "intake_guide_emailed":       "Emailed a broker how to send files on a channel",
    "file_arrival_released":      "Released a file on hold",
    # What the release / discard handlers write, with the file's name on it.
    "intake.arrival.released":    "Released a file on hold",
    "intake.arrival.discarded":   "Discarded a received file",
    "intake_arrival_rerun":       "Re-ran a received file",
    "onboarding_skipped":         "Skipped onboarding",
    # the received file's life after it lands (submission_service)
    "bordereau_sent":             "Sent the bordereau to the carrier",
    "bdx_submission_delivered":   "Delivered a received file to the carrier",
    "bdx_submission_on_hold":     "Put a received file on hold",
    "bdx_submission_deadline_hold": "Held a file because its deadline passed",
    "bdx_delivery_rule_updated":  "Changed a programme's delivery rule",
    # The status messages about a file — to the broker, or the carrier when a
    # deadline needs a decision. Which message, and to whom, is the detail.
    "bdx_notice_sent":            "Emailed a file status update",
    "bdx_notice_failed":          "Could not email a file status update",
    "bdx_notice_skipped":         "Did not send a file status update",
    # The broker's "exceptions review link" — the button in the "Exceptions to
    # resolve" email. Named after that email, because "the secure link" does
    # not say WHICH link; the Target column says which file it was for.
    "bdx_fix_link_code_sent":     "Asked for a sign-in code by email to open "
                                  "the exceptions review link",
    "bdx_fix_link_code_wrong":    "Entered a wrong sign-in code on the "
                                  "exceptions review link",
    "bdx_fix_link_opened":        "Signed in to the exceptions review link "
                                  "from their email",
    "bdx_fix_link_answers":       "Saved answers on the exceptions review link",
    # The broker pressed the page's "Validate" button: every rule run again on
    # a preview of the next version, their corrections applied. Nothing sent.
    "bdx_fix_link_validated":     "Validated their corrections by clicking "
                                  "“Validate” on the exceptions review link",
    "bdx_fix_link_submitted":     "Submitted their corrections from the "
                                  "exceptions review link",
    # the Rule Library switch (the request path cannot say which way it went)
    "rule_enabled":               "Switched a rule on",
    "rule_disabled":              "Switched a rule off",
    "rule_toggled":               "Switched a rule on or off",
    # access_log — reads and downloads of output / source data
    "download":         "Downloaded a file",
    "read":             "Opened a file to view it",
    "export":           "Exported data",
    # reminders written by the calendar sweep (actor: the system)
    "submission_overdue":   "Marked a bordereau as overdue",
    "submission_due_soon":  "Reminded that a bordereau is due soon",
    "submission_due_today": "Reminded that a bordereau is due today",
    "submission_chased":    "Chased a broker for a late bordereau",
}

# What the badge says, and how it is coloured. Everything not listed is read
# from the action name by _status_for().
_STATUS = {
    "login_success":    ("Successful", "ok"),
    "login_failed":     ("Failed", "bad"),
    "logout":           ("Signed out", "muted"),
    "forgot_request":   ("Reset requested", "info"),
    "password_reset":   ("Password reset", "info"),
    "password_changed": ("Password changed", "info"),
    "token_refresh":    ("Session renewed", "muted"),
    "exception_decided": ("Exception resolved", "warn"),
    # Without these the tail-matching below reads "_approved" as nothing
    # in particular and "_rejected" not at all. An approval decision is
    # the kind of row somebody scans a log FOR, so it says what it was.
    "bordereau_setup_submitted": ("Awaiting approval", "info"),
    "bordereau_setup_approved":  ("Approved", "ok"),
    "bordereau_setup_rejected":  ("Sent back", "warn"),
    "broker_request_submitted":  ("Awaiting approval", "info"),
    "broker_request_approved":   ("Approved", "ok"),
    # "Turned down", not "Sent back": there is nothing to send back. Nothing
    # was created, so the request ends here and a new one starts the ask again.
    "broker_request_rejected":   ("Turned down", "bad"),
    "broker_request_withdrawn":  ("Withdrawn", "muted"),
    "contract_sent_back":        ("Sent back", "warn"),
    "contract_awaiting_review":  ("Awaiting review", "info"),
    "contract_awaiting_signature": ("Awaiting signature", "warn"),
    "contract_accepted":         ("Accepted", "ok"),
    "contract_pushed_back":      ("Changes requested", "warn"),
    # The busiest gate on the contract road had no badge at all, so the moment
    # the broker agrees rendered as a grey "Completed" beside every other row.
    "contract_terms_accepted":   ("Terms agreed", "ok"),
    "contract_signed_off":       ("Signed", "ok"),
    # Read by name rather than by tail: "_released" / "_discarded" / "_sent"
    # match nothing below and would all read as a grey "Completed".
    "intake.arrival.released":   ("Released", "ok"),
    "intake.arrival.discarded":  ("Discarded", "muted"),
    "intake_key_revoked":        ("Revoked", "bad"),
    "rule_enabled":              ("Switched on", "ok"),
    "rule_disabled":             ("Switched off", "muted"),
    "bordereau_sent":            ("Sent", "ok"),
    "bdx_submission_delivered":  ("Delivered", "ok"),
    "bdx_notice_sent":           ("Sent", "ok"),
    "bdx_notice_failed":         ("Failed", "bad"),
    "bdx_notice_skipped":        ("Not sent", "muted"),
    "bdx_fix_link_code_wrong":   ("Wrong code", "bad"),
    "bdx_fix_link_submitted":    ("Submitted", "ok"),
}

_METHOD_WORDS = {"POST": "Created", "PUT": "Updated", "PATCH": "Updated", "DELETE": "Deleted"}
_ID = re.compile(r"^(?:\w+):(\d+)$")


def _humanize(action: str) -> str:
    """A readable phrase for an action nobody has named yet.

    Two shapes arrive here: a dotted/underscored event (`rule.tolerance_changed`)
    and the middleware's fallback for an unnamed mutation (`POST /programs/{id}`).
    """
    if " " in action:                                  # "POST /programs/{id}"
        method, _, path = action.partition(" ")
        what = re.sub(r"/\{[a-z]+\}", "", path.strip("/"))
        what = what.replace("/", " ").replace("-", " ").replace("_", " ")
        return f"{_METHOD_WORDS.get(method, method.title())} {what}".strip()
    words = action.replace(".", " ").replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else "Activity"


def canonical_action(action: str) -> str:
    """The event name behind a middleware fallback ("POST /esign/sign/x/verify"
    -> "signing_code_verified"), or the action itself."""
    if not action or action in ACTION_WORDS or " " not in action:
        return action
    method, _, path = action.partition(" ")
    try:
        from audit import _FRIENDLY, normalize_path
        return _FRIENDLY.get((method, normalize_path(path))) or action
    except Exception:  # noqa: BLE001 — wording must never break a read
        return action


def action_words(action: str) -> str:
    """The words for one action, however it happens to be spelled.

    A row written before an endpoint was named still carries the middleware's
    fallback, "POST /contracts/{id}/accept-terms". Rather than keep a second
    table of those, they are resolved through the SAME map the writer now uses
    (audit._FRIENDLY) and then worded — so naming an endpoint once makes its
    whole history readable, not just the rows written after it.
    """
    known = ACTION_WORDS.get(action)
    if known:
        return known
    named = canonical_action(action)
    if named != action:
        return ACTION_WORDS.get(named, _humanize(named))
    return _humanize(action)


# Kavachio doing something by itself — nobody pressed a button. Said as such
# ("Kavachio · Automatic"), never as an anonymous "System": a reader new to the
# trail must be able to tell a step the platform takes from one a person took.
AUTOMATIC_ACTION_WORDS = {
    "direct_output_generated": "Process Bordereau: Checked the received file and built the output BDX",
    "output_generated":        "Process Bordereau: Checked the received file and built the output BDX",
    "datamodel.ingest":        "Loaded the file's rows into the data model",
    "bordereau_sent":          "Sent the bordereau on to the carrier by email",
    "contract.rules_generated": "Built the checks for a contract from its clauses",
    "datamodel_mapping_proposed": "Suggested how a file's columns map to the data model",
}


def notice_words(action: str, details: Any) -> Optional[str]:
    """A file status email, named by its subject: "Emailed “Exceptions to
    resolve” to the broker" says what the broker received; "Sent a file
    status update" made them open the row to find out."""
    d = details if isinstance(details, dict) else {}
    bits = str(d.get("key") or "").split(":")
    status = bits[2] if len(bits) > 2 else None
    event = d.get("event")
    title = audit_names.NOTICE_WORDS.get(
        event if event in ("deadline_hold", "carrier_deadline") else status or "")
    if not title:
        return None
    to = "the carrier" if event == "carrier_deadline" else "the broker"
    if d.get("channel") == "sftp":
        verb = {"bdx_notice_sent": "Put", "bdx_notice_failed": "Could not put",
                "bdx_notice_skipped": "Did not put"}.get(action, "Put")
        return f"{verb} a “{title}” status file on {to}'s SFTP"
    verb = {"bdx_notice_sent": "Emailed", "bdx_notice_failed": "Could not email",
            "bdx_notice_skipped": "Did not email"}.get(action, "Emailed")
    return f"{verb} “{title}” to {to}"


# What was downloaded or opened, said by WHAT it is — "Downloaded a file" next
# to a path told nobody which file.
_ACCESS_WORDS = [
    (re.compile(r"^/export/downloads/\d+/file"), "Downloaded the output BDX file"),
    (re.compile(r"^/carriers/\d+/programs/\d+/brokers/\d+/contracts/\d+/runs/\d+/file"),
     "Downloaded the output BDX file"),
    (re.compile(r"^/export/downloads/\d+/data"), "Opened a processed bordereau's rows"),
    # The exceptions screen loading the file — 303 of the 350 "downloads" on
    # this database (6 Oct 2026). A look, not a copy taken away.
    (re.compile(r"^/export/downloads/\d+"),      "Opened a processed bordereau to review it"),
    (re.compile(r"^/uploads/\d+/file"),          "Downloaded the original file as received"),
    (re.compile(r"^/uploads/\d+/source-rows"),   "Opened the original file's rows"),
    (re.compile(r"^/mapper/\d+/file"),           "Downloaded a sample file"),
    (re.compile(r"^/audit/export"),              "Downloaded the audit log"),
    (re.compile(r"^/dwh"),                       "Opened the data model"),
]


def access_words(action: str, resource: Optional[str]) -> str:
    for rx, words in _ACCESS_WORDS:
        if resource and rx.match(resource):
            return words if action == "download" or "Opened" in words \
                else words.replace("Downloaded", "Opened")
    return action_words(action)


DOWNLOAD_RESOURCES = r"(/file$|^/audit/export$)"


def _access_status(label: str) -> tuple[str, str]:
    """The badge says what the words say: a file taken away, or a look."""
    return ("Downloaded", "info") if label.startswith("Downloaded") else ("Viewed", "muted")


def _status_for(category: str, action: str, details: Any, ok: Any = None) -> tuple[str, str]:
    """(badge text, tone). Tone is one of ok | warn | info | bad | muted."""
    # "Sent" on a bordereau whose email never left would be the one badge on
    # the page that is wrong; the row records whether it went.
    if action == "bordereau_sent" and isinstance(details, dict) \
            and details.get("mail_sent") is False:
        return "Email not sent", "warn"
    if action in _STATUS:
        return _STATUS[action]
    if category == "auth":
        return ("Successful", "ok") if ok in (None, True) else ("Failed", "bad")
    if category == "access":
        return ("Downloaded", "info") if action == "download" else ("Viewed", "muted")
    # An activity row. A run's own verdict beats anything derived from the name.
    d = details if isinstance(details, dict) else {}
    run_status = d.get("status")
    if isinstance(run_status, str):
        low = run_status.lower()
        if low in ("clean", "ok", "passed"):
            return "Clean", "ok"
        if low in ("flagged", "exceptions"):
            return "Flagged", "warn"
        if low in ("not_validated", "not_checked"):
            return "Not Validated", "muted"
    for tail, words, tone in (
        ("_uploaded", "File uploaded", "ok"),
        ("_generated", "Generated", "ok"),
        ("_activated", "Activated", "ok"),
        ("_created", "Created", "ok"),
        ("_invited", "Invite sent", "info"),
        ("_deleted", "Deleted", "bad"),
        ("_removed", "Removed", "bad"),
        ("_updated", "Updated", "info"),
        ("_saved", "Saved", "info"),
        ("_resolved", "Resolved", "warn"),
    ):
        if action.endswith(tail):
            return words, tone
    if action.startswith("submission_"):
        return "Reminder", "warn"
    return "Completed", "muted"


# What each kind of decision IS, and how it reads. Only `fix` appears on this
# database so far; the other three are what the decide endpoints can write.
# Said by the BUTTON that was used — Fix, Approve, Dismiss — and what it did to
# the value, so "Corrected a value" no longer leaves the reader to guess which.
DECISION_WORDS = {
    "fix":     "Fixed an exception — changed the value",
    "approve": "Approved an exception — kept the value as it is",
    "dismiss": "Dismissed an exception — no change to the value",
    "reject":  "Rejected an exception",
}
DECISION_STATUS = {
    "fix":     ("Fixed", "warn"),
    "approve": ("Approved", "ok"),
    "dismiss": ("Dismissed", "muted"),
    "reject":  ("Rejected", "bad"),
}

# Keys already shown in a column of their own, or that tell a reader nothing:
# the HTTP status is the badge, the IP has its own column, and the actor's email
# is the actor.
_DETAIL_SKIP = {"status", "ip", "method", "email", "filename", "file", "name",
                "template_name", "full_name", "user_agent"}

_DETAIL_LABELS = {
    "rows": "{v} rows", "policies": "{v} policies", "exceptions": "{v} exceptions",
    "updated": "{v} updated", "skipped": "{v} skipped", "loaded": "{v} loaded",
    "failed": "{v} failed", "sheet_count": "{v} sheets", "count": "{v} items",
    "version": "version {v}", "lane": "{v} lane", "reason": "{v}",
    "role": "role {v}", "stage": "stage {v}", "resolved": "{v} resolved",
}


# Why a sign-in failed, in words. The stored code stays in the export's
# "Event name" world; the screen says what it means.
_AUTH_REASONS = {
    "invalid_credentials": "Wrong email or password",
}


def _clip(value, n: int = 60) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= n else text[: n - 1] + "\u2026"


def _detail_words(details: Any, kind: str = "activity") -> str:
    """The specifics of one event, in a phrase.

    An audit line that says only "Generated the output BDX" has not recorded
    anything anybody can check. Whatever the event stored about itself — how
    many rows, how many exceptions, which lane — belongs on the row, beside it.

    A sign-in is the exception: auth_audit stores the person's own name and role
    alongside the event, and both are already the Actor column. Repeating them
    as "role carrier_admin, full name sayaj" adds width, not information, so an
    auth row says only why it failed, when it did.
    """
    d = details if isinstance(details, dict) else {}
    if kind == "auth":
        reason = d.get("reason") or d.get("error")
        if not reason:
            return ""
        return _AUTH_REASONS.get(str(reason), _clip(str(reason).replace("_", " ").capitalize(), 120))
    parts = []
    for key, value in d.items():
        if key in _DETAIL_SKIP or value is None or value == "" or value == []:
            continue
        if isinstance(value, (list, tuple, set)):
            # A list of records (the open exceptions a delivery carried, 6 Oct
            # 2026) is counted, not printed — and never sorted: dicts do not
            # compare, and one such row took the whole page down.
            if any(isinstance(x, (dict, list, tuple)) for x in value):
                value = f"{len(value)} items"
            else:
                value = ", ".join(sorted((str(x) for x in value if x), key=str))
            if not value:
                continue
        elif isinstance(value, dict):
            continue
        fmt = _DETAIL_LABELS.get(key)
        parts.append(fmt.format(v=_clip(value, 80)) if fmt
                     else f"{key.replace('_', ' ')} {_clip(value, 80)}")
    return " \u00b7 ".join(parts[:5])


def _decision_detail(r) -> str:
    """Exactly which cell was decided, and what it became.

    This is the whole point of an audit line for a decision: "Resolved 2
    exceptions" records that something happened, not what. The decision log
    holds the policy, the sheet, the row, the field and both values — so the
    line says them.
    """
    where = []
    if r.policy_number:
        where.append(f"policy {r.policy_number}")
    if r.sheet:
        where.append(f"{r.sheet}, row {r.row}" if r.row is not None else str(r.sheet))
    elif r.row is not None:
        where.append(f"row {r.row}")
    joined = " \u00b7 ".join(where)
    out = (r.field or "value") + (f" on {joined}" if joined else "")
    if (r.kind or "").lower() == "fix" and (r.old_value is not None or r.new_value is not None):
        out += f": \u201c{_clip(r.old_value, 48)}\u201d \u2192 \u201c{_clip(r.new_value, 48)}\u201d"
    if r.reason:
        out += f" \u2014 {_clip(r.reason, 80)}"
    return out


# An access_log `resource` is the request path that was read. These name the
# thing behind the path, so the Target column reads like a list of documents
# rather than a server log.
_RESOURCE_WORDS = [
    (re.compile(r"^/export/downloads/(\d+)/file$"),        "Output BDX #{id} (file)"),
    (re.compile(r"^/export/downloads/(\d+)/data$"),        "Output BDX #{id} (data)"),
    (re.compile(r"^/export/downloads/(\d+)$"),             "Output BDX #{id}"),
    (re.compile(r"^/uploads/(\d+)/file$"),                 "Uploaded file #{id}"),
    (re.compile(r"^/uploads/(\d+)/source-rows$"),          "Source rows of upload #{id}"),
    (re.compile(r"^/uploads/(\d+)$"),                      "Upload #{id}"),
    (re.compile(r"^/mapper/(\d+)/file$"),                  "Mapper source file #{id}"),
    (re.compile(r"^/dwh$"),                                "The data model"),
    (re.compile(r"^/audit/export$"),                       "The audit trail"),
]


def _target_words(target: Optional[str], details: Any) -> str:
    """What the action was done TO, in words worth reading.

    A filename beats everything — it is what the person recognises. Failing
    that, the named-event targets (`export:Motor BDX`, `exceptions:12`) already
    read well; only the middleware's raw request path needs collapsing, and it
    is shown with its ids folded away so the column groups instead of sprawling.
    """
    d = details if isinstance(details, dict) else {}
    for key in ("filename", "file", "name", "template_name"):
        val = d.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    if not target:
        return "—"
    t = target.strip()
    if t.startswith("/"):
        # A signing link already written to a row before it was redacted at the
        # source is still a live credential. Mask it on the way out too, so the
        # fix covers the rows that are already there.
        try:
            from audit import redact_path
            t = redact_path(t)          # the token only — ids are what names it
        except Exception:  # noqa: BLE001
            pass
        # An access_log resource is a request path. Name what was read rather
        # than echoing the URL: "/export/downloads/355/file" tells the reader
        # nothing they were looking for.
        for pattern, words in _RESOURCE_WORDS:
            m = pattern.match(t)
            if m:
                return words.format(id=m.group(1) if m.groups() else "")
        # A path with no ids in it says exactly what the Action column already
        # says — "/output-template/analyze-sources" beside "Analysed the source
        # files" is the same sentence twice, in worse words.
        if not re.search(r"/\d+", t):
            return "—"
        # It still carries an id, which IS worth showing: which programme, which
        # export. Read it out as words rather than as a URL.
        words = re.sub(r"/(\d+)", r" #\1", t.strip("/")).replace("/", " \u00b7 ")
        words = words.replace("-", " ").replace("_", " ")
        return words[:1].upper() + words[1:]
    if t.startswith("exceptions:"):
        n = _ID.match(t)
        return f"{n.group(1)} exception(s)" if n else t
    if ":" in t:
        kind, _, rest = t.partition(":")
        return rest if rest and not rest.isdigit() else f"{kind.title()} #{rest}"
    return t


# ---------------------------------------------------------------------------
# naming the actor, for one viewer
# ---------------------------------------------------------------------------

# Actor strings that are Kavachio itself, never a person: the auto-run, the
# calendar sweep, the status-email sender, and background jobs that wrote a
# placeholder (or nothing) where a person would go.
_AUTOMATIC_ACTORS = {"", "system", "auto-run", "kavachio", "default", "crosscheck"}

# Events recorded against a broker COMPANY with nobody behind them, because
# nobody pressed anything: the bordereau goes on to the carrier by itself after
# a run (submission_calendar_service.send_bordereau), and a delivered run loads
# the data model in the background. The broker did not do these; Kavachio did,
# for them.
_AUTOMATIC_BROKER_ACTIONS = {"bordereau_sent", "datamodel.ingest"}

# Written by whoever holds an emailed e-signing link — no login, so no seat.
_SIGNER_ACTIONS = {"contract_signed_from_email", "signing_code_verified",
                   "signing_code_resent", "contract_signature_declined"}

# How the exceptions review link names itself on the rows its Validate writes.
_LINK_ACTOR = re.compile(r"^Secure link \((.+)\)$")


class ActorNamer:
    """Turns an acting seat into the words THIS viewer may see.

    Built once per response, so each person, carrier and broker is looked up
    once however many rows they appear on.
    """

    def __init__(self, s, viewer: Viewer):
        self.s = s
        self.v = viewer
        self._users: dict[int, Optional[AppUser]] = {}
        self._by_email: dict[str, Optional[AppUser]] = {}
        self._parties: dict[int, Optional[str]] = {}
        self._tenants: dict[int, Optional[str]] = {}
        self._owners: dict[int, Optional[int]] = {}

    # --- lookups -----------------------------------------------------------
    def user(self, uid) -> Optional[AppUser]:
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            return None
        if uid not in self._users:
            self._users[uid] = self.s.get(AppUser, uid)
        return self._users[uid]

    def user_by_email(self, email: Optional[str]) -> Optional[AppUser]:
        if not email or "@" not in email:
            return None
        if email not in self._by_email:
            self._by_email[email] = (self.s.query(AppUser)
                                     .filter(AppUser.email == email).first())
        return self._by_email[email]

    def party_name(self, pid) -> Optional[str]:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return None
        if pid not in self._parties:
            p = self.s.get(Party, pid)
            self._parties[pid] = getattr(p, "legal_name", None)
        return self._parties[pid]

    def tenant_name(self, tid) -> Optional[str]:
        try:
            tid = int(tid)
        except (TypeError, ValueError):
            return None
        if tid not in self._tenants:
            t = self.s.get(Tenant, tid)
            self._tenants[tid] = (getattr(t, "legal_name", None)
                                  or getattr(t, "tenant_name", None))
            self._owners[tid] = getattr(t, "owner_user_id", None)
        return self._tenants[tid]

    def _carrier_role(self, u: AppUser) -> str:
        """Carrier Admin or Carrier User — the owner pointer decides, the same
        way it decides in the sidebar."""
        self.tenant_name(u.tenant_id)              # populates _owners
        owner = self._owners.get(u.tenant_id)
        return "carrier_admin" if (owner is None or owner == u.id) else "carrier_user"

    # --- the answer --------------------------------------------------------
    def _broker_role(self, org: Optional[str], r: str = "broker_admin") -> str:
        """"Broker · Wani org" — which broker company the person belongs to.
        A broker reading their own team's trail knows the company already."""
        base = ROLE_WORDS.get(r, ROLE_WORDS["broker_admin"])
        return f"{base} · {org}" if org and not self.v.is_broker else base

    def _automatic(self, org: Optional[str]) -> dict:
        """Kavachio itself — a step nobody pressed a button for. Said as what it
        is, and for whom when a broker is involved, never as a bare "System"."""
        role = "Automatic"
        if org and not self.v.is_broker:
            role += f" · for {org}"
        return {"name": "Kavachio", "role": role, "role_key": "system", "org": None}

    def name(self, *, user_id=None, actor: Optional[str] = None,
             broker_party_id=None, role: Optional[str] = None,
             via_email: Optional[str] = None, action: Optional[str] = None) -> dict:
        """{name, role, role_key, org} for one row, as this viewer may see it.

        `via_email` is the address an emailed link was sent to (the exceptions
        review link), for the rows a link writes with no login behind them:
        the link is that person's, so the row is theirs.
        """
        act = canonical_action(action or "")
        m = _LINK_ACTOR.match(actor or "")
        if m and not via_email:
            via_email = m.group(1).strip()
        u = self.user(user_id) or self.user_by_email(actor)
        # Some writers record the person as their bare user id ("2419") — the
        # held-file release and discard did. That is a person, not automation.
        if u is None and isinstance(actor, str) and actor.isdigit():
            u = self.user(actor)

        pid = broker_party_id
        if pid is None and isinstance(actor, str) and actor.startswith("broker:"):
            mm = _ID.match(actor)
            pid = int(mm.group(1)) if mm else None

        if u is None and via_email:
            cand = self.user_by_email(via_email)
            # The link speaks for ONE broker; the same address seated at
            # another organisation is somebody else's seat.
            if cand is not None and (pid is None or cand.broker_party_id is None
                                     or int(cand.broker_party_id) == int(pid)):
                u = cand

        if u is None:
            org = self.party_name(pid) if pid else None
            if via_email:
                # The link went to an address with no Kavachio login (a channel
                # contact), or a person since removed. The address IS who it was.
                return {"name": via_email, "role": self._broker_role(org),
                        "role_key": "broker_admin", "org": org}
            if user_id not in (None, ""):
                # A seat was recorded and has since been deleted (the retired
                # broker-user seats, 29 Sep) — a person, never Kavachio.
                return {"name": "A removed user",
                        "role": self._broker_role(org) if org else "Not a current user",
                        "role_key": "former", "org": org}
            if isinstance(actor, str) and actor.startswith("broker:"):
                if act in _AUTOMATIC_BROKER_ACTIONS:
                    return self._automatic(org)
                # Written before the person was recorded: the company is all
                # the row knows, and it says so rather than guess.
                return {"name": org or "Broker", "role": "Broker · person not recorded",
                        "role_key": "broker_admin", "org": org}
            if act in _SIGNER_ACTIONS:
                return {"name": "Contract signer", "role": "Emailed signing link",
                        "role_key": "signer", "org": None}
            a = (actor or "").strip()
            if "@" in a:
                # An account since removed — or, on a failed sign-in, an
                # address that never was one.
                return {"name": a, "role": "Not a current user", "role_key": "former",
                        "org": None}
            if a and a.lower() not in _AUTOMATIC_ACTORS and not a.lower().startswith("kavachio"):
                # A name typed onto a run ("acceltree") rather than a login.
                return {"name": a, "role": "Name given on the run",
                        "role_key": "named", "org": None}
            return self._automatic(org)

        r = normalize_role(role or u.role)
        person = u.full_name or u.email

        if r in BROKER_ROLES:
            org = self.party_name(u.broker_party_id or pid)
            # A retired broker-user seat ("operator"): the carrier dealt with
            # the company, never with those seats, and still does.
            if r != "broker_admin" and self.v.is_carrier:
                return {"name": org or "Broker", "role": ROLE_WORDS["broker_admin"],
                        "role_key": "broker_admin", "org": org}
            # The broker's own person, named — the carrier invited them by
            # name and needs to know WHO at the broker did it.
            return {"name": person, "role": self._broker_role(org, r),
                    "role_key": r, "org": org}

        if r == "kavachio_admin":
            return {"name": person if self.v.is_platform else "Kavachio",
                    "role": ROLE_WORDS[r], "role_key": r, "org": "Kavachio"}

        seat = self._carrier_role(u)
        org = self.tenant_name(u.tenant_id)
        # A broker never reads a carrier person's trail today (nothing puts one
        # in their scope), but if that changes the company is what they get.
        if self.v.is_broker:
            return {"name": org or "The carrier", "role": ROLE_WORDS["carrier_admin"],
                    "role_key": "carrier_admin", "org": org}
        return {"name": person, "role": ROLE_WORDS[seat], "role_key": seat, "org": org}


# Rows that should never have been written. These three endpoints are reads —
# two render a preview, one marks your own notifications as seen — and the
# middleware recorded them as mutations. They are 1,898 of the 14,186 activity
# rows on this database: 13% of the whole trail, and on the days they were made
# they bury everything else on the first page.
#
# audit.py no longer writes them. Nothing is deleted — an audit table is
# append-only — they are simply not part of the feed, the same way a session
# renewal is not. Remove an entry here and its history reappears.
SUPPRESSED_ACTIONS = (
    "POST /contract-wording/preview",
    "POST /contracts/{id}/endorsement/preview",
    "POST /admin/notifications/read",
    # The middleware's copy of an event the handler ALSO wrote, with the file's
    # or rule's name on it. Every one of these (checked 6 Oct 2026: 9 releases,
    # 4 discards, 2 key revocations, 3 rule switches) has exactly that named
    # twin, written in the same second, so each action read twice — once as
    # "Intake · arrivals #46 · release". audit.py no longer writes them.
    "file_arrival_released",
    "POST /intake/arrivals/{id}/discard",
    "DELETE /intake/keys/{id}",
    "PATCH /rule-library/{id}",
    # Same again for switching a channel on or off (4 of 4 twinned with
    # intake_route_updated, 6 Oct 2026) — read as "Updated intake routes".
    "PATCH /intake/routes/{id}",
)

# Twins that are NEVER read, whatever the filter — the same click recorded
# twice, where the other copy says more. Kept in the table (the review link
# counts its own answers from it); left out of the trail.
#   bdx_fix_link_answers   every answer saved on the exceptions review link is
#                          ALSO one exception_decision_log row with the policy,
#                          the field and both values (439 of 439, 6 Oct 2026).
#   direct_output_checked  written by the review link's Validate in the same
#   by "Secure link (…)"   millisecond as bdx_fix_link_validated, which has the
#                          counts that matter (corrected, still failing, open).
NEVER_SHOWN_ACTIONS = ("bdx_fix_link_answers",)
# The bordereau email to the carrier is switched off on purpose
# (BORDEREAU_AUTO_SEND_EMAIL unset): send_bordereau still releases the version
# and writes its bordereau_sent row with this mail_error, but no email was
# ever attempted, so there is nothing to show (6 Oct 2026, user request). A
# real failure — any other mail_error — is still shown.
_MAIL_SWITCHED_OFF = "outbound email is temporarily disabled"

# The middleware's copy of a save that ALSO wrote its own named row. These copy
# the event NAME (audit._FRIENDLY), so they are told apart by their target: the
# request path. 6 Oct 2026: 6 of 6 schedule copies had their named twin in the
# same second. audit.py no longer writes them (_SELF_LOGGED).
_PATH_TWINS = (("submission_schedule_updated", "/programs/%/schedule"),)

# The deadline check's reminders. One check marks EVERY late month of a
# programme at once (a new programme starting last October: eleven at once),
# so the screen shows one line per check and programme, opening onto the
# months — the way a bulk update opens onto its changes.
REMINDER_ACTIONS = ("submission_overdue", "submission_due_soon", "submission_due_today")
REMINDER_GROUP_WORDS = {
    "submission_overdue":   "Marked {n} bordereaux as overdue",
    "submission_due_soon":  "Reminded that {n} bordereaux are due soon",
    "submission_due_today": "Reminded that {n} bordereaux are due today",
}
_LINK_CHECK_TWIN = ("direct_output_checked", "Secure link (%")


# The Action Type dropdown: the work grouped the way people talk about it,
# rather than forty raw event names. One definition, read by both the dropdown
# (options) and the filter that the choice turns into (actions_for_group).
# Every seat, for a group that belongs to all of them.
ALL_SEATS = ("platform", "carrier_admin", "carrier_user", "broker_admin", "broker_user")
# The four that do the carrier's work, plus Kavachio.
CARRIER_SEATS = ("platform", "carrier_admin", "carrier_user")

ACTION_GROUPS: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("all", "All actions", (), ALL_SEATS),
    ("auth", "Sign-in and security",
     ("login_success", "login_failed", "logout", "forgot_request",
      "password_reset", "password_changed"), ALL_SEATS),
    ("files", "Files and runs",
     ("bdx_uploaded", "direct_setup_uploaded", "supplement_uploaded",
      "contract_uploaded", "output_generated", "direct_output_generated",
      "direct_output_checked", "validation.run", "datamodel.ingest",
      "bordereau_run", "bordereau_sent", "bdx_submission_delivered",
      "bdx_submission_on_hold", "bdx_submission_deadline_hold",
      "bdx_notice_sent", "bdx_notice_failed", "bdx_notice_skipped"), ALL_SEATS),
    # The decisions themselves live in exception_decision_log; the secure-link
    # rows are the broker answering the same exceptions from an email.
    ("exceptions", "Exceptions",
     ("exception_decided", "bdx_fix_link_code_sent", "bdx_fix_link_code_wrong",
      "bdx_fix_link_opened", "bdx_fix_link_answers", "bdx_fix_link_validated",
      "bdx_fix_link_submitted"), ALL_SEATS),
    # Building a Bordereau Setup, a mapping or an output template is the
    # carrier's work. A broker runs against what the carrier built and never
    # touches one, so offering them this filter offers a guaranteed blank page.
    ("setup", "Setup and templates",
     ("bordereau_setup_completed", "bordereau_setup_activated", "bordereau_setup_needed",
      # The approval trail. CARRIER_SEATS, like the rest of this category: the
      # decision is internal to the carrier, and the broker only ever sees the
      # setup that came out the other side.
      "bordereau_setup_submitted", "bordereau_setup_approved",
      "bordereau_setup_rejected",
      "bordereau_setup_created", "bdx_setup_updated", "bdx_setup_deleted",
      "sheet_bindings_saved", "input_mapper_generated", "input_mapper_updated",
      "input_mapper_activated", "output_template_generated",
      "output_template_updated", "output_template_activated",
      "output_template_refreshed", "output_template_fields_saved",
      "output_template_from_standard", "output_sources_analyzed",
      "canonical_fields_saved", "missing_columns_analyzed"), CARRIER_SEATS),
    # A broker ADMIN is a party to the contract — they accept its terms, ask
    # for changes and sign it — so this is theirs too. The rules on it are the
    # carrier's, but they hang off the same record. An operator never sees a
    # contract at all.
    ("contracts", "Contracts and rules",
     ("contract_uploaded", "contract.rules_generated", "contract.rules_built",
      "clause.resolved_to_field", "rule_created", "rule_updated", "rule_deleted",
      "rule.disabled", "rule.tolerance_changed", "rule.output_field_changed",
      "rule.variation_value_added", "rule.variation_value_removed",
      "rule_enabled", "rule_disabled", "rule_toggled",
      "contract_raised", "contract_edited", "contract_submitted",
      "contract_sent_for_review", "contract_sent_back",
      "contract_awaiting_review", "contract_awaiting_signature",
      "contract_accepted", "contract_signed_off", "contract_terms_accepted",
      "contract_pushed_back",
      "contract_changes_requested", "contract_approved", "contract_rejected",
      "contract_activated", "contract_terminated", "contract_renewed",
      "contract_document_attached", "contract_wording_saved",
      "contract_wording_drafted", "contract_endorsed", "contract_checks_bound",
      "contract_mapped_to_template", "contract_review_skipped"),
     CARRIER_SEATS + ("broker_admin",)),
    ("signing", "Signing",
     ("signature_round_started", "contract_signed", "contract_signed_from_email",
      "signing_code_verified", "contract_signed_copy_submitted",
      "signature_envelope_created", "signature_envelope_sent",
      "signing_code_resent", "contract_signature_declined",
      "signature_reminder_sent", "signature_round_voided"),
     CARRIER_SEATS + ("broker_admin",)),
    # Who is on the platform. A broker admin staffs their own team, so they get
    # it; an operator is a seat in that team, not a manager of it.
    ("people", "People and access",
     ("user_invited", "invite_resent", "user_updated", "user_deleted",
      "profile_updated", "ownership_transferred", "broker_user_created",
      "broker_invitation_accepted", "broker_invitation_declined",
      "onboarding_skipped"),
     CARRIER_SEATS + ("broker_admin",)),
    # The carrier's own mesh: which brokers exist and which programmes they are
    # on. A broker is put ON these, and does none of it.
    ("brokers", "Brokers and programmes",
     ("broker_added", "broker_linked", "broker_put_on_programme",
      # The approval trail in front of them. CARRIER_SEATS like the rest of
      # this category: the decision is internal to the carrier, and a broker
      # who was turned down must never be able to learn they were considered.
      "broker_request_submitted", "broker_request_approved",
      "broker_request_rejected", "broker_request_withdrawn",
      "broker_invitation_withdrawn", "broker_removed_from_programme",
      "program_created", "program_updated", "party_created", "party_updated",
      "party_contact_added", "party_contact_removed",
      "submission_schedule_updated", "submission_chased",
      "bdx_delivery_rule_updated"), CARRIER_SEATS),
    # How files reach the CARRIER — mailboxes, routes and keys. The broker
    # sends; it is the carrier that sets up the ways in.
    ("intake", "Ingestion channels",
     ("intake_route_created", "intake_route_updated",
      "intake_route_contacts_updated", "intake_key_created", "intake_key_revoked",
      "mailbox_polled", "intake_sftp_tested", "intake_guide_sent",
      "intake_guide_emailed", "intake.arrival.released",
      "intake.arrival.discarded", "intake_arrival_rerun"), CARRIER_SEATS),
    ("downloads", "Downloads and views", ("download", "read", "export"), ALL_SEATS),
    # The browser silently renewing its token. Kept out of the default feed
    # (see _rows) because it is the machine working, not a person acting — but
    # it is real security evidence, so it stays one click away.
    ("sessions", "Session renewals", ("token_refresh",), ALL_SEATS),
)


def groups_for_seat(seat: str) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """The Action Type choices this seat may be offered.

    A broker user was being shown "Setup and templates" and "Contracts and
    rules" — filters for work they have no part in, which can only ever return
    an empty table. The dropdown is a list of questions this person can
    sensibly ask, so it is built from what their seat actually does.
    """
    return tuple((k, lbl, acts) for k, lbl, acts, seats in ACTION_GROUPS
                 if seat in seats)


# ---------------------------------------------------------------------------
# the query
# ---------------------------------------------------------------------------

@dataclass
class Filters:
    since: Optional[datetime] = None
    until: Optional[datetime] = None
    categories: tuple[str, ...] = CATEGORIES
    actions: tuple[str, ...] = ()
    actor_user_ids: tuple[int, ...] = ()
    actor_broker_ids: tuple[int, ...] = ()
    actor_system: bool = False           # rows with no signed-in actor
    q: str = ""
    # `token_refresh` is the browser renewing its own token every hour — the
    # machine working, not a person acting, and on a real tenant it is most of
    # auth_audit. Kept out of the unfiltered feed so the trail reads as things
    # people did; the "Session renewals" action type brings it back.
    include_session_renewals: bool = False


def _actor_user_col(model):
    """The column holding the acting user id, whichever store this is.

    activity_events names it `actor_user_id` (added with this screen); auth_audit
    and access_log have carried `user_id` all along. Spelled out rather than
    written as `a or b`, which would lean on the truthiness of a SQLAlchemy
    column — defined today, and exactly the kind of thing that stops being.
    """
    col = getattr(model, "actor_user_id", None)
    return col if col is not None else getattr(model, "user_id", None)


def _scope_clause(model, sc: Scope):
    """Rows this scope may read, for one of the three tables.

    Three ways in, because the three stores record the actor differently and
    activity_events changed shape mid-life:
      * the acting user id          — every row written since the Audit screen
      * the acting broker party     — same, and it survives the `broker:<id>`
                                      display masking
      * the `actor` display string  — older rows, matched on email or broker key
    Plus, on activity_events read by a carrier ADMIN, the tenant itself: a row
    written for the carrier by something with no seat (the calendar sweep) still
    belongs to that carrier's trail.
    """
    if sc.everything:
        return None
    ors = []
    if sc.user_ids:
        col = _actor_user_col(model)
        if col is not None:
            ors.append(col.in_(sorted(sc.user_ids)))
    broker_col = getattr(model, "actor_broker_party_id", None)
    if broker_col is not None and sc.broker_party_ids:
        ors.append(broker_col.in_(sorted(sc.broker_party_ids)))
    known = sc.emails | sc.broker_actor_keys
    if known:
        ors.append(model.actor.in_(sorted(known)))
    if sc.tenant_ids and hasattr(model, "tenant_id"):
        ors.append(model.tenant_id.in_(sorted(sc.tenant_ids)))
    if not ors:
        return None if sc.everything else False
    return or_(*ors)


def _actor_filter_clause(model, f: Filters):
    """The Role/Actor dropdown, applied in SQL."""
    ors = []
    if f.actor_user_ids:
        col = _actor_user_col(model)
        if col is not None:
            ors.append(col.in_(f.actor_user_ids))
        # Older rows carry the email only, so resolve the chosen people to
        # theirs — otherwise picking a person hides their own history.
        emails = _emails_of(f.actor_user_ids)
        ors.append(model.actor.in_(emails))
        # The exceptions review link has no login: its rows name the person by
        # the address the link was emailed to, and they are that person's too.
        if model is ActivityEvent and emails:
            ors.append(model.actor.in_([f"Secure link ({e})" for e in emails]))
            ors.append(cast(model.details, JSONB)["email"].astext.in_(emails))
    if f.actor_broker_ids:
        broker_col = getattr(model, "actor_broker_party_id", None)
        if broker_col is not None:
            ors.append(broker_col.in_(f.actor_broker_ids))
        ors.append(model.actor.in_([f"broker:{b}" for b in f.actor_broker_ids]))
        ors.append(_in_broker_users(model, f.actor_broker_ids))
    if f.actor_system:
        # Exactly the rows the table names "Kavachio · Automatic" — not every
        # row without an email on it, which took in the review link's rows
        # (the broker) and the held-file release (the carrier, by user id).
        col = _actor_user_col(model)
        auto = or_(model.actor.is_(None),
                   func.lower(model.actor).in_(sorted(_AUTOMATIC_ACTORS)),
                   func.lower(model.actor).like("kavachio%"))
        if model is ActivityEvent:
            auto = or_(auto, and_(model.actor.like("broker:%"),
                                  model.action.in_(sorted(_AUTOMATIC_BROKER_ACTIONS))))
        ors.append(and_(col.is_(None), auto) if col is not None else auto)
    return or_(*[o for o in ors if o is not None]) if ors else None


_email_memo: dict[tuple[int, ...], list[str]] = {}


def _emails_of(user_ids: Iterable[int]) -> list[str]:
    key = tuple(sorted(user_ids))
    if key not in _email_memo:
        with SessionLocal() as s:
            rows = s.query(AppUser.email).filter(AppUser.id.in_(key)).all()
        _email_memo[key] = [r[0] for r in rows if r[0]]
    return _email_memo[key]


def _in_broker_users(model, broker_ids: Iterable[int]):
    """Rows whose actor is one of these brokers' people, matched by user id —
    covers a broker seat that recorded its own email rather than the company."""
    col = _actor_user_col(model)
    if col is None:
        return None
    with SessionLocal() as s:
        uids = [r[0] for r in s.query(AppUser.id)
                .filter(AppUser.broker_party_id.in_(list(broker_ids))).all()]
    return col.in_(uids) if uids else None


def _q_clause(model, q: str, uids: list[int], bids: list[int]):
    """Free-text search. Display names live in app_user / party, not on the
    audit row, so the people and brokers matching the text are resolved first
    and folded in as ids — searching a name then finds every row that person
    wrote, and the totals stay honest."""
    like = f"%{q}%"
    ors = [model.actor.ilike(like)]
    for col_name in ("action", "event", "resource", "target"):
        col = getattr(model, col_name, None)
        if col is not None:
            ors.append(col.ilike(like))
    if uids:
        col = _actor_user_col(model)
        if col is not None:
            ors.append(col.in_(uids))
    if bids:
        broker_col = getattr(model, "actor_broker_party_id", None)
        if broker_col is not None:
            ors.append(broker_col.in_(bids))
        ors.append(model.actor.in_([f"broker:{b}" for b in bids]))
    return or_(*ors)


def _search_targets(s, q: str) -> tuple[list[int], list[int]]:
    """(user ids, broker party ids) whose NAME matches the search text."""
    like = f"%{q}%"
    uids = [r[0] for r in s.query(AppUser.id).filter(
        or_(AppUser.full_name.ilike(like), AppUser.email.ilike(like))).limit(500).all()]
    bids = [r[0] for r in s.query(Party.id).filter(
        Party.legal_name.ilike(like)).limit(500).all()]
    return uids, bids


def _base_query(s, model, sc: Scope, f: Filters, q_ids):
    q = s.query(model)
    clause = _scope_clause(model, sc)
    if clause is False:
        return None
    if clause is not None:
        q = q.filter(clause)
    if f.since is not None:
        q = q.filter(model.created_at >= f.since)
    if f.until is not None:
        q = q.filter(model.created_at <= f.until)
    actor_clause = _actor_filter_clause(model, f)
    if actor_clause is not None:
        q = q.filter(actor_clause)
    if f.q:
        q = q.filter(_q_clause(model, f.q, *q_ids))
    return q


def _decision_scope_clause(sc: Scope):
    """Who may read an exception decision. Same rule as everywhere else, against
    this table's own column names — it records the decider directly, so there is
    no display string to fall back on and nothing to resolve at read time."""
    if sc.everything:
        return None
    ors = []
    if sc.user_ids:
        ors.append(ExceptionDecisionLog.decided_by_user_id.in_(sorted(sc.user_ids)))
    if sc.broker_party_ids:
        ors.append(ExceptionDecisionLog.decided_by_broker_party_id.in_(sorted(sc.broker_party_ids)))
    if sc.tenant_ids:
        ors.append(ExceptionDecisionLog.tenant_id.in_(sorted(sc.tenant_ids)))
    return or_(*ors) if ors else False


def _brokers_of_users(user_ids: Iterable[int]) -> list[int]:
    with SessionLocal() as s:
        rows = (s.query(AppUser.broker_party_id)
                .filter(AppUser.id.in_(list(user_ids)),
                        AppUser.broker_party_id.isnot(None)).distinct().all())
    return [int(r[0]) for r in rows]


def _decision_query(s, sc: Scope, f: Filters, q_ids):
    """Every exception decision in scope, one stored row per decision.

    The screen shows a BULK save as one line (see _decision_groups) — with
    every decision in it one click away — and the download keeps one line per
    decision. Nothing is summed per person or per day: "Resolved 73 exceptions
    this week" records that something happened, not what.
    """
    E = ExceptionDecisionLog
    clause = _decision_scope_clause(sc)
    if clause is False:
        return None
    if f.actor_system:
        # Nobody signed in never decides an exception — a person always does.
        return None
    q = s.query(E)
    if clause is not None:
        q = q.filter(clause)
    if f.since is not None:
        q = q.filter(E.decided_at >= f.since)
    if f.until is not None:
        q = q.filter(E.decided_at <= f.until)
    if f.actor_user_ids:
        # A decision made on the exceptions review link records the broker
        # company and no seat (there is no login there) — it is the person the
        # link was emailed to, so choosing that person must not hide it.
        link = [E.decided_by_user_id.in_(f.actor_user_ids)]
        bids = _brokers_of_users(f.actor_user_ids)
        if bids:
            link.append(and_(E.decided_by_user_id.is_(None),
                             E.decided_by_broker_party_id.in_(bids)))
        q = q.filter(or_(*link))
    if f.actor_broker_ids:
        q = q.filter(E.decided_by_broker_party_id.in_(f.actor_broker_ids))
    if f.q:
        uids, bids = q_ids
        like = f"%{f.q}%"
        ors = [E.policy_number.ilike(like), E.field.ilike(like), E.sheet.ilike(like),
               E.kind.ilike(like), E.old_value.ilike(like), E.new_value.ilike(like)]
        if uids:
            ors.append(E.decided_by_user_id.in_(uids))
        if bids:
            ors.append(E.decided_by_broker_party_id.in_(bids))
        q = q.filter(or_(*ors))
    return q


def _decision_target(export_id, landing_id) -> Optional[str]:
    """A ref, named at render time: the file the decision was made on."""
    return (f"export:{export_id}" if export_id
            else f"landing:{landing_id}" if landing_id else None)


def _decision_raw(r) -> dict:
    """One stored decision as a feed row."""
    uid, bid = r.decided_by_user_id, r.decided_by_broker_party_id
    return {
        "kind": "decision", "id": r.id, "at": r.decided_at,
        "action": "exception_decided",
        "target": _decision_target(r.export_id, r.landing_id),
        "details": None, "detail": _decision_detail(r),
        "decision_kind": (r.kind or "").lower(), "group": None,
        # No seat but a broker: made on the exceptions review link.
        "actor": f"broker:{bid}" if uid is None and bid else None,
        "via_link": uid is None and bid is not None,
        "user_id": uid, "role": r.decided_by_role, "broker_party_id": bid,
        "tenant_id": r.tenant_id, "ip": None, "ok": None,
    }


# One bulk save writes every decision in it inside one request: microseconds
# apart, on consecutive ids (6 Oct 2026: 40 decisions within 63 µs). Two
# separate clicks are seconds apart. A second is the line between them.
BULK_GAP = timedelta(seconds=1)


def _decision_groups(s, q, limit: int) -> tuple[list[dict], int]:
    """The newest `limit` SAVES, newest first, and how many there are.

    A save is a run of decisions by the same person (or the same review link)
    on the same file with less than BULK_GAP between them. A save of one
    decision is shown exactly as before; a bulk save is ONE line that says
    what it did as a whole ("Corrected 40 values in one bulk update") and
    opens onto every decision in it (group_rows).
    """
    E = ExceptionDecisionLog
    base = q.with_entities(
        E.id.label("id"), E.decided_at.label("at"), E.export_id.label("export_id"),
        E.landing_id.label("landing_id"), E.decided_by_user_id.label("uid"),
        E.decided_by_broker_party_id.label("bid"), E.decided_by_role.label("role"),
        E.tenant_id.label("tenant_id"), E.kind.label("kind"), E.field.label("field"),
        E.sheet.label("sheet"), E.row.label("row"), E.policy_number.label("policy"),
        E.old_value.label("old"), E.new_value.label("new"), E.reason.label("reason"),
    ).subquery()
    part = (base.c.export_id, base.c.landing_id, base.c.uid, base.c.bid)
    prev = func.lag(base.c.at).over(partition_by=part, order_by=(base.c.at, base.c.id))
    starts = case((or_(prev.is_(None), base.c.at - prev > BULK_GAP), 1), else_=0)
    s1 = s.query(base, starts.label("starts")).subquery()
    p1 = (s1.c.export_id, s1.c.landing_id, s1.c.uid, s1.c.bid)
    grp = func.sum(s1.c.starts).over(partition_by=p1, order_by=(s1.c.at, s1.c.id))
    s2 = s.query(s1, grp.label("grp")).subquery()
    kind = func.lower(s2.c.kind)
    groups = (s.query(
        func.min(s2.c.id).label("lo"), func.max(s2.c.id).label("hi"),
        func.count().label("n"), func.max(s2.c.at).label("at"),
        s2.c.export_id, s2.c.landing_id, s2.c.uid, s2.c.bid,
        func.min(s2.c.role).label("role"), func.min(s2.c.tenant_id).label("tenant_id"),
        func.sum(case((kind == "fix", 1), else_=0)).label("n_fix"),
        func.sum(case((kind == "approve", 1), else_=0)).label("n_approve"),
        func.sum(case((kind == "dismiss", 1), else_=0)).label("n_dismiss"),
        func.sum(case((kind == "reject", 1), else_=0)).label("n_reject"),
        func.array_agg(s2.c.field.distinct()).label("fields"),
        func.array_agg(s2.c.sheet.distinct()).label("sheets"),
        func.min(s2.c.row).label("row_lo"), func.max(s2.c.row).label("row_hi"),
        func.count(s2.c.policy.distinct()).label("policies"),
        func.count(s2.c.new.distinct()).label("n_new"), func.min(s2.c.new).label("new"),
        func.count(s2.c.old.distinct()).label("n_old"), func.min(s2.c.old).label("old"),
        func.count(s2.c.reason.distinct()).label("n_reason"),
        func.min(s2.c.reason).label("reason"),
    ).group_by(s2.c.export_id, s2.c.landing_id, s2.c.uid, s2.c.bid, s2.c.grp)
        .subquery())
    total = s.query(func.count()).select_from(groups).scalar() or 0
    top = (s.query(groups).order_by(groups.c.at.desc(), groups.c.hi.desc())
           .limit(limit).all())

    # A save of one decision reads exactly as it always did.
    single = {r.id: r for r in s.query(E).filter(
        E.id.in_([g.lo for g in top if g.n == 1])).all()} if top else {}
    out = []
    for g in top:
        if g.n == 1 and g.lo in single:
            out.append(_decision_raw(single[g.lo]))
            continue
        counts = {k: int(getattr(g, f"n_{k}") or 0)
                  for k in ("fix", "approve", "dismiss", "reject")}
        kinds = [k for k, c in counts.items() if c]
        raw = {
            "kind": "decision", "id": g.lo, "at": g.at, "action": "exception_decided",
            "target": _decision_target(g.export_id, g.landing_id),
            "details": None, "detail": _group_detail(g, counts),
            "decision_kind": kinds[0] if len(kinds) == 1 else "mixed",
            "group": {"key": f"decision:{g.lo}-{g.hi}", "count": int(g.n),
                      "counts": counts},
            "actor": f"broker:{g.bid}" if g.uid is None and g.bid else None,
            "via_link": g.uid is None and g.bid is not None,
            "user_id": g.uid, "role": g.role, "broker_party_id": g.bid,
            "tenant_id": g.tenant_id, "ip": None, "ok": None,
        }
        out.append(raw)
    return out, int(total)


def _group_detail(g, counts: dict) -> str:
    """What a bulk save did, as a whole: which field(s), how many policies,
    where in the file, and — when it was the same everywhere — the value."""
    fields = sorted(f for f in (g.fields or []) if f)
    sheets = [x for x in (g.sheets or []) if x]
    parts = []
    if len(fields) == 1:
        what = fields[0]
    elif fields:
        what = f"{len(fields)} fields ({', '.join(fields[:3])}{'…' if len(fields) > 3 else ''})"
    else:
        what = "values"
    pol = int(g.policies or 0)
    parts.append(f"{what} on {pol} {'policy' if pol == 1 else 'policies'}" if pol else what)
    if len(sheets) == 1 and g.row_lo is not None:
        rows = (f"row {g.row_lo}" if g.row_lo == g.row_hi
                else f"rows {g.row_lo}–{g.row_hi}")
        parts.append(f"{sheets[0]}, {rows}")
    elif len(sheets) > 1:
        parts.append(f"{len(sheets)} sheets")
    if counts.get("fix") and g.n_new == 1 and g.new is not None:
        if g.n_old == 1 and g.old is not None:
            parts.append(f"“{_clip(g.old, 40)}” → "
                         f"“{_clip(g.new, 40)}” on every row")
        else:
            parts.append(f"all changed to “{_clip(g.new, 48)}”")
    if g.n_reason == 1 and g.reason:
        parts.append(f"Reason: {_clip(g.reason, 80)}")
    mixed = [f"{c} {w}" for k, w in (("fix", "fixed"), ("approve", "approved"),
                                     ("dismiss", "dismissed"), ("reject", "rejected"))
             if (c := counts.get(k))]
    if len(mixed) > 1:
        parts.append(", ".join(mixed))
    return " · ".join(parts)


def _decision_rows(s, sc: Scope, f: Filters, q_ids, limit: int,
                   grouped: bool = True) -> tuple[list[dict], int]:
    q = _decision_query(s, sc, f, q_ids)
    if q is None:
        return [], 0
    if grouped:
        return _decision_groups(s, q, limit)
    total = q.count()
    rows = q.order_by(desc(ExceptionDecisionLog.decided_at),
                      desc(ExceptionDecisionLog.id)).limit(limit)
    return [_decision_raw(r) for r in rows], total


def _link_people(s, raws: list[dict]) -> None:
    """Who made the decisions on an exceptions review link.

    The link has no login, so its decisions record the broker company and no
    seat. The review link's own "answers saved" row, written in the same
    request, names the address the link was emailed to — that is the person.
    Matched per broker, by time; sets `via_email` on each decision row found.
    """
    want = [r for r in raws if r.get("kind") == "decision" and r.get("via_link")
            and r.get("at") is not None and not r.get("via_email")]
    if not want:
        return
    bids = sorted({int(r["broker_party_id"]) for r in want})
    lo = min(r["at"] for r in want) - timedelta(seconds=5)
    hi = max(r["at"] for r in want) + timedelta(seconds=30)
    try:
        events = (s.query(ActivityEvent.created_at, ActivityEvent.actor_broker_party_id,
                          ActivityEvent.details)
                  .filter(ActivityEvent.action == "bdx_fix_link_answers",
                          ActivityEvent.actor_broker_party_id.in_(bids),
                          ActivityEvent.created_at.between(lo, hi)).all())
    except Exception:  # noqa: BLE001 — a name is a nicety; the row is not
        s.rollback()
        return
    by_broker: dict[int, list] = {}
    for at, bid, d in events:
        email = (d or {}).get("email") if isinstance(d, dict) else None
        if at is not None and bid is not None and email:
            by_broker.setdefault(int(bid), []).append((at, email))
    for r in want:
        cands = [(abs((at - r["at"]).total_seconds()), email)
                 for at, email in by_broker.get(int(r["broker_party_id"]), ())
                 if (at - r["at"]).total_seconds() >= -5]
        if cands:
            r["via_email"] = min(cands)[1]


# One click, two rows: the handler's NAMED row (what it was about, the counts)
# and the middleware's REQUEST COPY (target = the request path), sometimes under
# the same event name. The copy is hidden wherever its named twin exists — same
# person, within seconds. Copies with no twin (hundreds, written before their
# handler named its event) stay. 6 Oct 2026: channels 8/8, API keys 8/8,
# templates 5/5, contract rules 5/6, schedules 4/4, setups 3/3 twinned.
# A copy whose named row has a DIFFERENT name:
_NAMED_TWIN_OF = {
    "contract_changes_requested":    "contract_pushed_back",
    "contract_terms_accepted":       "contract_awaiting_signature",
    "output_template_from_standard": "output_template_generated",
    # A bordereau uploaded in the portal: the click and its result (with the
    # counts) are one action.
    "bordereau_run":                 "direct_output_generated",
}


def _without_named_twins(q):
    from sqlalchemy import literal, select
    from sqlalchemy.orm import aliased
    A, B = ActivityEvent, aliased(ActivityEvent)
    named = case(*[(A.action == c, literal(n)) for c, n in _NAMED_TWIN_OF.items()],
                 else_=A.action)
    same_person = or_(
        and_(A.actor_user_id.isnot(None), B.actor_user_id == A.actor_user_id),
        and_(or_(B.actor == A.actor, func.coalesce(B.actor, "") == ""),
             or_(B.tenant_id == A.tenant_id, and_(B.tenant_id.is_(None), A.tenant_id.is_(None)))))
    twin = (select(B.id).where(
        B.id != A.id,
        ~func.coalesce(B.target, "").like("/%"),
        B.action == named,
        B.created_at.between(A.created_at - timedelta(seconds=2),
                             A.created_at + timedelta(seconds=20)),
        same_person).correlate(A).exists())
    copy = and_(func.coalesce(A.target, "").like("/%"), twin)
    # A broker opening the contract to sign it: their signature is its own row.
    opening = and_(A.action.in_(("signature_round_started",
                                 "POST /esign/contracts/{id}/signing-session")),
                   func.coalesce(A.actor_role, "").in_(("broker_admin", "operator")))
    return q.filter(~or_(copy, opening))


def _twin_people(s, raws: list[dict]) -> None:
    """A named row written with no person (contract rules built in a worker)
    takes the person from its request copy — the one who pressed the button."""
    want = [r for r in raws if r.get("kind") == "activity" and not r.get("user_id")
            and not (r.get("actor") or "").strip() and r.get("at")
            and not str(r.get("target") or "").startswith("/")]
    if not want:
        return
    try:
        copies = (s.query(ActivityEvent.action, ActivityEvent.created_at,
                          ActivityEvent.actor, ActivityEvent.actor_user_id,
                          ActivityEvent.actor_role, ActivityEvent.tenant_id)
                  .filter(ActivityEvent.action.in_(sorted({r["action"] for r in want})),
                          ActivityEvent.target.like("/%"),
                          ActivityEvent.actor_user_id.isnot(None),
                          ActivityEvent.created_at.between(
                              min(r["at"] for r in want) - timedelta(seconds=2),
                              max(r["at"] for r in want) + timedelta(seconds=2))).all())
    except Exception:  # noqa: BLE001
        s.rollback()
        return
    for r in want:
        hit = next((c for c in copies if c.action == r["action"]
                    and c.tenant_id == r["tenant_id"]
                    and abs((c.created_at - r["at"]).total_seconds()) <= 2), None)
        if hit is not None:
            r["actor"], r["user_id"], r["role"] = hit.actor, hit.actor_user_id, hit.actor_role


def _reminder_groups(s, q, limit: int) -> tuple[list[dict], int]:
    """The deadline check's reminders, one row per check and programme.

    A check writes all its reminders for one programme in the same instant
    (microseconds apart); the next check is hours away. So rows of the same
    kind, for the same programme and carrier, less than BULK_GAP apart, are
    one check. A check that found one late month reads exactly as before.
    """
    A = ActivityEvent
    period = cast(A.details, JSONB)["period"].astext
    base = q.with_entities(A.id.label("id"), A.created_at.label("at"),
                           A.action.label("action"), A.target.label("target"),
                           A.tenant_id.label("tenant_id"), period.label("period")).subquery()
    part = (base.c.action, base.c.target, base.c.tenant_id)
    prev = func.lag(base.c.at).over(partition_by=part, order_by=(base.c.at, base.c.id))
    starts = case((or_(prev.is_(None), base.c.at - prev > BULK_GAP), 1), else_=0)
    s1 = s.query(base, starts.label("starts")).subquery()
    p1 = (s1.c.action, s1.c.target, s1.c.tenant_id)
    grp = func.sum(s1.c.starts).over(partition_by=p1, order_by=(s1.c.at, s1.c.id))
    s2 = s.query(s1, grp.label("grp")).subquery()
    groups = (s.query(func.min(s2.c.id).label("lo"), func.max(s2.c.id).label("hi"),
                      func.count().label("n"), func.max(s2.c.at).label("at"),
                      s2.c.action, s2.c.target, s2.c.tenant_id,
                      func.min(s2.c.period).label("p_lo"), func.max(s2.c.period).label("p_hi"))
              .group_by(s2.c.action, s2.c.target, s2.c.tenant_id, s2.c.grp).subquery())
    total = s.query(func.count()).select_from(groups).scalar() or 0
    top = (s.query(groups).order_by(groups.c.at.desc(), groups.c.hi.desc())
           .limit(limit).all())
    single = {e.id: e for e in s.query(A).filter(
        A.id.in_([g.lo for g in top if g.n == 1])).all()} if top else {}
    out = []
    for g in top:
        e = single.get(g.lo) if g.n == 1 else None
        if e is not None:
            out.append(_activity_raw(e))
            continue
        span = audit_names.period_words(g.p_lo) if g.p_lo else ""
        if g.p_hi and g.p_hi != g.p_lo:
            span += f" – {audit_names.period_words(g.p_hi)}"
        out.append({
            "kind": "activity", "id": g.lo, "at": g.at, "action": g.action,
            "target": g.target, "details": {}, "detail": "",
            # Bordereaux, not months: a programme can have two due for the same
            # month (programme 521 has, Sep 2026).
            "group_detail": span or "",
            "group": {"key": f"reminder:{g.lo}-{g.hi}", "count": int(g.n),
                      "noun": "bordereaux"},
            "actor": "system", "user_id": None, "role": None, "broker_party_id": None,
            "via_email": None, "tenant_id": g.tenant_id, "ip": None, "ok": None,
        })
    return out, int(total)


def _safe_detail(details) -> str:
    """The fallback wording, which must never fail a page: one row nobody
    foresaw is worth a blank line, not "Could not load the audit trail"."""
    try:
        return _detail_words(details)
    except Exception:  # noqa: BLE001
        return ""


def _activity_raw(e) -> dict:
    """One stored activity row as a feed row."""
    return {
        "kind": "activity", "id": e.id, "at": e.created_at,
        "action": e.action, "target": e.target, "details": e.details,
        "detail": _safe_detail(e.details),
        "actor": e.actor, "user_id": getattr(e, "actor_user_id", None),
        "role": getattr(e, "actor_role", None),
        "broker_party_id": getattr(e, "actor_broker_party_id", None),
        "via_email": _link_email(e.details),
        "tenant_id": e.tenant_id, "ip": (e.details or {}).get("ip")
        if isinstance(e.details, dict) else None, "ok": None,
    }


def _link_versions(s, raws: list[dict]) -> None:
    """The GENUINE version numbers on the exceptions review link's rows.

    A link row stored the number its file had at the time — and on 3 Oct 2026
    submissions were merged across channels and renumbered (migrations 32–34),
    so a row from 1 Oct says "Version 2" of a file now numbered 5 everywhere
    else. So the number is worked out from the file's history as it is NOW:
    the version the link was open on is the newest version of that submission
    made before the row; the next one takes the next number. Read from
    output_exports, written by the same clock as the audit rows.
    """
    want = [r for r in raws if r.get("kind") == "activity"
            and str(r.get("action") or "").startswith("bdx_fix_link_")
            and str(r.get("target") or "").startswith("submission:") and r.get("at")]
    if not want:
        return
    from db import OutputExport as O
    refs = sorted({r["target"].split(":", 1)[1] for r in want})
    try:
        hist = (s.query(O.submission_ref, O.created_at, O.version_no, O.version_status)
                .filter(O.submission_ref.in_(refs), O.version_no.isnot(None))
                .order_by(O.created_at).all())
    except Exception:  # noqa: BLE001 — the stored number is the fallback
        s.rollback()
        return
    by_ref: dict[str, list] = {}
    for ref, at, no, status in hist:
        if at is not None:
            by_ref.setdefault(ref, []).append((at, int(no), status))
    for r in want:
        rows = by_ref.get(r["target"].split(":", 1)[1])
        if not rows:
            continue
        before = [x for x in rows if x[0] <= r["at"]]
        if not before:
            continue
        on = before[-1][1]
        nxt = max(x[1] for x in before) + 1
        d = dict(r["details"]) if isinstance(r["details"], dict) else {}
        act = r["action"]
        if act == "bdx_fix_link_validated":
            d["version"] = nxt                  # the preview IS the next version
        elif act == "bdx_fix_link_submitted":
            # What the submit actually became: the correction made seconds later.
            made = next((x for x in rows if x[0] > r["at"] and x[2] is not None
                         and (x[0] - r["at"]).total_seconds() < 600), None)
            d["from_version"] = on
            d["to_version"] = made[1] if made else nxt
            d.pop("version", None)
        else:
            d["version"] = on
        r["details"] = d


_RUN_ACTIONS = ("direct_output_generated", "direct_output_checked", "output_generated")


def _run_exports(s, raws: list[dict]) -> None:
    """A portal run's row, tied to the processed bordereau it made.

    The run row records only the output's file name, so three uploads of the
    same file read as the same line three times. The export written in the
    same instant (same clock, same name) says which file it was for — period
    and version — and whether the person had just uploaded it.
    """
    want = [r for r in raws if r.get("kind") == "activity" and r["action"] in _RUN_ACTIONS
            and r.get("at") and isinstance(r.get("details"), dict)
            and r["details"].get("filename")]
    if not want:
        return
    from db import OutputExport as O
    lo = min(r["at"] for r in want) - timedelta(seconds=10)
    hi = max(r["at"] for r in want) + timedelta(seconds=10)
    try:
        outs = (s.query(O.id, O.created_at, O.filename)
                .filter(O.created_at.between(lo, hi),
                        O.filename.in_(sorted({r["details"]["filename"] for r in want})))
                .all())
        uids = sorted({int(r["user_id"]) for r in want if r.get("user_id")})
        runs = (s.query(ActivityEvent.actor_user_id, ActivityEvent.created_at)
                .filter(ActivityEvent.action == "bordereau_run",
                        ActivityEvent.actor_user_id.in_(uids),
                        ActivityEvent.created_at.between(lo - timedelta(seconds=10), hi))
                .all()) if uids else []
    except Exception:  # noqa: BLE001
        s.rollback()
        return
    for r in want:
        near = [(abs((o.created_at - r["at"]).total_seconds()), o.id) for o in outs
                if o.filename == r["details"]["filename"] and o.created_at is not None
                and abs((o.created_at - r["at"]).total_seconds()) <= 5]
        if near:
            # Named after the file that was SENT, with its programme, period
            # and version — the output's own name says none of that.
            r["target"] = f"export:{min(near)[1]}"
            r["details"] = {k: v for k, v in r["details"].items() if k != "filename"}
        if r["action"] == "direct_output_generated" and r.get("user_id") and any(
                uid == r["user_id"] and 0 <= (r["at"] - at).total_seconds() <= 15
                for uid, at in runs):
            r["ran_upload"] = True


_SIGN_ACTIONS = ("contract_signed_from_email", "signing_code_verified",
                 "signing_code_resent", "contract_signature_declined")


def _sign_targets(s, raws: list[dict]) -> None:
    """Which contract an in-portal signature was for.

    The signing request's path carries only a (now blanked) token, so these
    rows named nothing. In the portal, the same person opened that contract to
    sign it moments before — /esign/contracts/<id>/signing-session — and that
    row has the contract's id. Emailed-link signers have no login and stay as
    they are.
    """
    want = [r for r in raws if r.get("kind") == "activity" and r.get("user_id")
            and r.get("at") and canonical_action(r["action"]) in _SIGN_ACTIONS]
    if not want:
        return
    uids = sorted({int(r["user_id"]) for r in want})
    lo = min(r["at"] for r in want) - timedelta(minutes=30)
    hi = max(r["at"] for r in want)
    try:
        opens = (s.query(ActivityEvent.actor_user_id, ActivityEvent.created_at,
                         ActivityEvent.target)
                 .filter(ActivityEvent.actor_user_id.in_(uids),
                         ActivityEvent.target.like("/esign/contracts/%/signing-session"),
                         ActivityEvent.created_at.between(lo, hi))
                 .order_by(ActivityEvent.created_at).all())
    except Exception:  # noqa: BLE001
        s.rollback()
        return
    for r in want:
        prior = [(at, t) for uid, at, t in opens
                 if uid == r["user_id"] and at <= r["at"]
                 and (r["at"] - at).total_seconds() <= 1800]
        if prior:
            cid = prior[-1][1].split("/")[3]
            if cid.isdigit():
                r["target"] = f"contract:{cid}"


def _link_email(details: Any) -> Optional[str]:
    """The address an exceptions review link was emailed to, on the rows the
    link writes itself (details.via == "secure link") — or the person added
    to an older row afterwards, from evidence (details.person_added_later)."""
    d = details if isinstance(details, dict) else {}
    if d.get("via") == "secure link" and isinstance(d.get("email"), str):
        return d["email"].strip() or None
    later = d.get("person_added_later")
    if isinstance(later, dict) and isinstance(later.get("email"), str):
        return later["email"].strip() or None
    return None


def _rows(s, sc: Scope, f: Filters, limit: int,
          grouped: bool = True) -> tuple[list[dict], int]:
    """The newest `limit` rows across every selected store, plus the true total.

    `grouped`: a bulk save of exception decisions is one row (the screen);
    off, every decision is its own row (the download)."""
    q_ids = _search_targets(s, f.q) if f.q else ([], [])
    out: list[dict] = []
    total = 0

    if "decision" in f.categories:
        d_rows, d_total = _decision_rows(s, sc, f, q_ids, limit, grouped=grouped)
        out.extend(d_rows)
        total += d_total

    if "activity" in f.categories:
        q = _base_query(s, ActivityEvent, sc, f, q_ids)
        if q is not None:
            if f.actions:
                q = q.filter(ActivityEvent.action.in_(f.actions))
            # Never from here: exception_decision_log is the same event with the
            # person on it (see the note at the top of this file). Reading both
            # would list every resolution twice, once as "the broker".
            q = q.filter(ActivityEvent.action != "exception_decided")
            q = q.filter(ActivityEvent.action.notin_(NEVER_SHOWN_ACTIONS))
            q = q.filter(~and_(
                ActivityEvent.action == "bordereau_sent",
                func.coalesce(cast(ActivityEvent.details, JSONB)["mail_error"].astext, "")
                == _MAIL_SWITCHED_OFF))
            q = q.filter(~and_(ActivityEvent.action == _LINK_CHECK_TWIN[0],
                               func.coalesce(ActivityEvent.actor, "").like(_LINK_CHECK_TWIN[1])))
            q = _without_named_twins(q)
            for twin_action, twin_path in _PATH_TWINS:
                q = q.filter(~and_(ActivityEvent.action == twin_action,
                                   func.coalesce(ActivityEvent.target, "").like(twin_path)))
            if not f.actions:
                q = q.filter(ActivityEvent.action.notin_(SUPPRESSED_ACTIONS))
            if grouped:
                r_rows, r_total = _reminder_groups(
                    s, q.filter(ActivityEvent.action.in_(REMINDER_ACTIONS)), limit)
                out.extend(r_rows)
                total += r_total
                q = q.filter(ActivityEvent.action.notin_(REMINDER_ACTIONS))
            total += q.count()
            for e in q.order_by(desc(ActivityEvent.created_at), desc(ActivityEvent.id)).limit(limit):
                out.append(_activity_raw(e))

    if "auth" in f.categories:
        q = _base_query(s, AuthAudit, sc, f, q_ids)
        if q is not None:
            if f.actions:
                q = q.filter(AuthAudit.event.in_(f.actions))
            elif not f.include_session_renewals:
                q = q.filter(AuthAudit.event != "token_refresh")
            total += q.count()
            for e in q.order_by(desc(AuthAudit.created_at), desc(AuthAudit.id)).limit(limit):
                out.append({
                    "kind": "auth", "id": e.id, "at": e.created_at,
                    "action": e.event, "target": None, "details": e.details,
                    "detail": _detail_words(e.details, "auth"),
                    "actor": e.actor, "user_id": e.user_id, "role": None,
                    "broker_party_id": None, "tenant_id": e.tenant_id,
                    "ip": e.ip, "ok": e.ok,
                })

    if "access" in f.categories:
        q = _base_query(s, AccessLog, sc, f, q_ids)
        if q is not None:
            if f.actions:
                q = q.filter(AccessLog.action.in_(f.actions))
            # Genuine downloads only — a file taken away, not a screen showing
            # one. New rows say which (action "download" vs "view"); older rows
            # were all written as "download", so for those the path decides:
            # a /file (or the audit export) is a download, the exceptions
            # screen loading /export/downloads/{id} or its /data is a view.
            q = q.filter(AccessLog.action == "download",
                         AccessLog.resource.op("~")(DOWNLOAD_RESOURCES))
            total += q.count()
            for e in q.order_by(desc(AccessLog.created_at), desc(AccessLog.id)).limit(limit):
                out.append({
                    "kind": "access", "id": e.id, "at": e.created_at,
                    "action": e.action, "target": e.resource, "details": None,
                    "detail": "",
                    "actor": e.actor, "user_id": e.user_id, "role": None,
                    "broker_party_id": None, "tenant_id": e.tenant_id,
                    "ip": e.ip, "ok": None,
                })

    # Merged newest-first. `at` can be None on a row written before the default
    # landed; those sort last rather than crashing the comparison.
    out.sort(key=lambda r: (r["at"] or datetime.min, r["id"]), reverse=True)
    return out, total


def _iso(dt) -> Optional[str]:
    """An explicit-UTC ISO string. Stored timestamps are naive UTC, and without
    the marker the browser reads them as local time — five and a half hours
    wrong on this team's machines."""
    if dt is None:
        return None
    text = dt.isoformat()
    return text if (text.endswith("Z") or "+" in text) else text + "Z"


def _names_for(s, v: Viewer, raws: list[dict]) -> "audit_names.Names":
    """One id->name resolver for a page, with every record on it fetched in
    bulk — a query per KIND of record, not one per row."""
    names = audit_names.Names(s, v)
    try:
        names.prefetch([ref for r in raws if r["kind"] != "auth"
                        for ref in audit_names.row_refs(r["action"], r["target"],
                                                        r["details"])])
    except Exception:  # noqa: BLE001 — names are a nicety; the rows are not
        pass
    return names


def _target_and_detail(names, raw: dict) -> tuple[Optional[str], str, str]:
    """(kind of thing, Target words, "what exactly") for one row.

    Ids become names; a request path becomes the record it acted on; the
    details lose their internal keys. Never fails a read: if naming goes wrong
    the row falls back to the words it had before.
    """
    kind = raw["kind"]
    try:
        if kind == "auth":
            return None, "—", raw.get("detail") or ""
        tkind, target = audit_names.target_words(names, raw["action"], raw["target"],
                                                 raw["details"])
        if kind == "activity":
            detail = audit_names.detail_words(names, raw["action"], raw["details"], target)
        else:
            detail = raw.get("detail") or ""
        return tkind, target, detail
    except Exception:  # noqa: BLE001
        return None, _target_words(raw["target"], raw["details"]), raw.get("detail") or ""


# A bulk save, said as one thing.
GROUP_WORDS = {
    "fix":     "Fixed {n} exceptions in one bulk update — changed the values",
    "approve": "Approved {n} exceptions in one bulk update — kept the values",
    "dismiss": "Dismissed {n} exceptions in one bulk update",
    "reject":  "Rejected {n} exceptions in one bulk update",
    "mixed":   "Resolved {n} exceptions in one bulk update",
}


def _label(raw: dict, who: dict) -> str:
    """The Action words for one row — by what it was, and who did it."""
    act = canonical_action(raw["action"])
    if raw["kind"] == "access":
        return access_words(raw["action"], raw["target"])
    if act.startswith("bdx_notice_"):
        return notice_words(act, raw["details"]) or action_words(act)
    if who["role_key"] == "system" and act in AUTOMATIC_ACTION_WORDS:
        return AUTOMATIC_ACTION_WORDS[act]
    if act == "signature_round_started" and str(raw.get("target") or "").startswith("contract:"):
        # Written by the handler only when a round really starts (6 Oct 2026).
        return "Started the signing round for the contract — the carrier signs first"
    return action_words(raw["action"])


def render(s, namer: ActorNamer, raw: dict, names=None) -> dict:
    """One stored row, as the screen shows it."""
    who = namer.name(user_id=raw["user_id"], actor=raw["actor"],
                     broker_party_id=raw["broker_party_id"], role=raw["role"],
                     via_email=raw.get("via_email"), action=raw["action"])
    status, tone = _status_for(raw["kind"], raw["action"], raw["details"], raw["ok"])
    label = _label(raw, who)
    if raw["kind"] == "access":
        status, tone = _access_status(label)
    group = raw.get("group")
    if raw["kind"] == "decision":
        kind = raw.get("decision_kind") or "fix"
        if group:
            label = GROUP_WORDS.get(kind, GROUP_WORDS["mixed"]).format(n=group["count"])
            status, tone = DECISION_STATUS.get(kind, ("Resolved", "warn"))
        else:
            label = DECISION_WORDS.get(kind, "Decided an exception")
            status, tone = DECISION_STATUS.get(kind, ("Exception resolved", "warn"))
    if names is None:
        names = audit_names.Names(s, namer.v)
    target_kind, target, detail = _target_and_detail(names, raw)
    if group and raw["kind"] == "activity" and raw["action"] in REMINDER_GROUP_WORDS:
        label = REMINDER_GROUP_WORDS[raw["action"]].format(n=group["count"])
        detail = raw.get("group_detail") or detail
    if raw["kind"] == "decision":
        # Where it was done: the link in the broker's email (no login), or the
        # exceptions screen in the portal.
        where = ("on the exceptions review link" if raw.get("via_link")
                 else "on the exceptions screen in the portal")
        detail = " · ".join(p for p in (detail, where) if p)
    if raw.get("ran_upload"):
        # The upload and its result are one row (see _run_exports).
        label = "Uploaded a bordereau in the portal and ran the checks"
    return {
        "id": group["key"] if group else f"{raw['kind']}:{raw['id']}",
        # A bulk save: how many decisions it holds, and the key that opens them.
        "group": {"key": group["key"], "count": group["count"],
                  "noun": group.get("noun", "changes")} if group else None,
        "at": _iso(raw["at"]),
        "category": raw["kind"],
        "actor": who["name"],
        "actor_role": who["role"],
        "actor_role_key": who["role_key"],
        "actor_org": who["org"],
        "action": raw["action"],
        "action_label": label,
        "detail": detail,
        # What KIND of thing the target is ("Contract", "Received file") —
        # the words alone are often just a name, and a name is ambiguous.
        "target_kind": target_kind,
        "target": target,
        "status": status,
        "tone": tone,
        "ip": raw["ip"],
        "carrier": namer.tenant_name(raw["tenant_id"]) if raw["tenant_id"] else None,
        # The download only: "Change 3 of 19 in one bulk update".
        "bulk": raw.get("bulk"),
    }


def page(s, v: Viewer, f: Filters, page_no: int, page_size: int) -> dict:
    """One page of the feed, newest first, already in the viewer's words.
    A bulk save of exception decisions is one row here (see group_rows)."""
    page_no = max(1, page_no)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    offset = min((page_no - 1) * page_size, MAX_OFFSET)
    sc = scope_for(s, v)
    raw, total = _rows(s, sc, f, offset + page_size)
    namer = ActorNamer(s, v)
    shown = raw[offset:offset + page_size]
    _link_people(s, shown)
    _link_versions(s, shown)
    _run_exports(s, shown)
    _sign_targets(s, shown)
    _twin_people(s, shown)
    names = _names_for(s, v, shown)
    items = [render(s, namer, r, names) for r in shown]
    return {"items": items, "total": total, "page": page_no, "page_size": page_size,
            "seat": v.seat}


def export_rows(s, v: Viewer, f: Filters) -> list[dict]:
    """Every row the current filters select, capped, for the download — one
    line per decision, never a bulk summary: a file kept as the record must
    say every value that changed without anybody having to click."""
    sc = scope_for(s, v)
    raw, _total = _rows(s, sc, f, MAX_EXPORT_ROWS, grouped=False)
    namer = ActorNamer(s, v)
    rows = raw[:MAX_EXPORT_ROWS]
    _tag_bulk(rows)
    _link_people(s, rows)
    _link_versions(s, rows)
    _run_exports(s, rows)
    _sign_targets(s, rows)
    _twin_people(s, rows)
    names = _names_for(s, v, rows)
    return [render(s, namer, r, names) for r in rows]


def _runs(rows: list[dict], key) -> list[list[dict]]:
    """Sorted rows split wherever the key changes or BULK_GAP passes."""
    runs: list[list[dict]] = []
    for r in rows:
        last = runs[-1][-1] if runs else None
        if last is not None and key(last) == key(r) and r["at"] - last["at"] <= BULK_GAP:
            runs[-1].append(r)
        else:
            runs.append([r])
    return runs


def _tag_bulk(raws: list[dict]) -> None:
    """For the download: which bulk save each decision row belongs to, by the
    same rule the screen groups by (same decider, same file, < BULK_GAP), so
    the 19 lines of "Corrected 19 values in one bulk update" read as one."""
    key = lambda r: (r["target"] or "", r["user_id"] or 0, r["broker_party_id"] or 0)
    dec = sorted((r for r in raws if r["kind"] == "decision" and r.get("at")),
                 key=lambda r: (key(r), r["at"], r["id"]))
    rem = sorted((r for r in raws if r["kind"] == "activity" and r.get("at")
                  and r["action"] in REMINDER_ACTIONS),
                 key=lambda r: (r["action"], r["target"] or "", r["tenant_id"] or 0,
                                r["at"], r["id"]))
    for run in _runs(rem, lambda r: (r["action"], r["target"] or "", r["tenant_id"] or 0)):
        if len(run) > 1:
            for i, r in enumerate(run, 1):
                r["bulk"] = f"{i} of {len(run)} bordereaux in one deadline check"
    runs: list[list[dict]] = []
    for r in dec:
        last = runs[-1][-1] if runs else None
        if last is not None and key(last) == key(r) and r["at"] - last["at"] <= BULK_GAP:
            runs[-1].append(r)
        else:
            runs.append([r])
    for run in runs:
        if len(run) > 1:
            for i, r in enumerate(run, 1):
                r["bulk"] = f"Change {i} of {len(run)} in one bulk update"


MAX_GROUP_ROWS = 5_000
GROUP_PAGE = 50
_REMINDER_KEY = re.compile(r"^reminder:(\d+)-(\d+)$")


def _reminder_rows(s, v: Viewer, f: Filters, key: str, offset: int, limit: int) -> dict:
    """The months behind one "Marked 11 bordereaux as overdue" line — the same
    scope and filters as the page, the same kind, programme and carrier as the
    check's first row, inside its id range."""
    empty = {"items": [], "total": 0}
    m = _REMINDER_KEY.match(key or "")
    if not m:
        return empty
    lo, hi = int(m.group(1)), int(m.group(2))
    if hi < lo or hi - lo > MAX_GROUP_ROWS:
        return empty
    A = ActivityEvent
    sc = scope_for(s, v)
    q_ids = _search_targets(s, f.q) if f.q else ([], [])
    q = _base_query(s, A, sc, f, q_ids)
    if q is None:
        return empty
    first = q.filter(A.id == lo, A.action.in_(REMINDER_ACTIONS)).first()
    if first is None:
        return empty
    members = q.filter(A.id.between(lo, hi), A.action == first.action,
                       A.target == first.target if first.target is not None
                       else A.target.is_(None),
                       A.tenant_id == first.tenant_id if first.tenant_id is not None
                       else A.tenant_id.is_(None))
    total = members.count()
    offset = max(0, int(offset or 0))
    limit = max(1, min(int(limit or GROUP_PAGE), 500))
    raws = [_activity_raw(e) for e in
            members.order_by(A.id).offset(offset).limit(limit).all()]
    namer = ActorNamer(s, v)
    names = _names_for(s, v, raws)
    return {"items": [render(s, namer, r, names) for r in raws], "total": total,
            "offset": offset}
_GROUP_KEY = re.compile(r"^decision:(\d+)-(\d+)$")


def group_rows(s, v: Viewer, f: Filters, key: str, offset: int = 0,
               limit: int = GROUP_PAGE) -> dict:
    """Every decision in one bulk save — what "View all" opens.

    The key is the save's first and last decision id. Membership is worked out
    again here, through the SAME scope and filters as the page, so a key typed
    by hand opens nothing the viewer could not already read: only decisions in
    that id range, by the same decider, on the same file, in scope.
    """
    empty = {"items": [], "total": 0}
    if (key or "").startswith("reminder:"):
        return _reminder_rows(s, v, f, key, offset, limit)
    m = _GROUP_KEY.match(key or "")
    if not m:
        return empty
    lo, hi = int(m.group(1)), int(m.group(2))
    if hi < lo or hi - lo > MAX_GROUP_ROWS:
        return empty
    E = ExceptionDecisionLog
    sc = scope_for(s, v)
    q_ids = _search_targets(s, f.q) if f.q else ([], [])
    q = _decision_query(s, sc, f, q_ids)
    if q is None:
        return empty
    first = q.filter(E.id == lo).first()
    if first is None:
        return empty

    def same(col, val):
        return col.is_(None) if val is None else col == val

    members = q.filter(E.id.between(lo, hi),
                       same(E.export_id, first.export_id),
                       same(E.landing_id, first.landing_id),
                       same(E.decided_by_user_id, first.decided_by_user_id),
                       same(E.decided_by_broker_party_id, first.decided_by_broker_party_id))
    total = members.count()
    # A page at a time — the drawer's "Load more" asks for the next one.
    offset = max(0, int(offset or 0))
    limit = max(1, min(int(limit or GROUP_PAGE), 500))
    rows = (members.order_by(E.sheet, E.row, E.id).offset(offset).limit(limit).all())
    raws = [_decision_raw(r) for r in rows]
    _link_people(s, raws)
    namer = ActorNamer(s, v)
    names = _names_for(s, v, raws)
    items = [render(s, namer, r, names) for r in raws]
    return {"items": items, "total": total, "offset": offset}


# ---------------------------------------------------------------------------
# what the filters may offer
# ---------------------------------------------------------------------------

def options(s, v: Viewer) -> dict:
    """The contents of the two dropdowns, built from what this viewer can
    actually see — so nobody is offered a filter that can only return nothing,
    and no name leaks through a dropdown that the table itself would mask."""
    sc = scope_for(s, v)
    namer = ActorNamer(s, v)

    actors: list[dict] = []
    seen: set[str] = set()

    def add(key: str, label: str, role: str, sort: tuple):
        if key in seen or not label:
            return
        seen.add(key)
        actors.append({"key": key, "label": label, "role": role, "_sort": sort})

    if sc.everything:
        people = s.query(AppUser).order_by(AppUser.full_name).limit(1000).all()
    elif sc.user_ids:
        people = (s.query(AppUser).filter(AppUser.id.in_(sorted(sc.user_ids)))
                  .order_by(AppUser.full_name).limit(1000).all())
    else:
        people = []

    for u in people:
        r = normalize_role(u.role)
        who = namer.name(user_id=u.id, role=r)
        # The table names a broker's own person (Broker · Wani org), so the
        # dropdown offers them by name too. A retired broker-user seat still
        # reads as its company to a carrier, so it folds into the company.
        key = (f"broker:{u.broker_party_id}"
               if (r in BROKER_ROLES and r != "broker_admin" and v.is_carrier
                   and u.broker_party_id)
               else f"user:{u.id}")
        add(key, who["name"], who["role"], (who["role"], who["name"]))

    # Every broker in reach, as a whole company, even one whose people have
    # never acted — "what has this broker been doing" covers all its people,
    # and the steps Kavachio took on its behalf.
    if v.is_carrier:
        for bid in sorted(sc.broker_party_ids):
            add(f"broker:{bid}", namer.party_name(bid) or f"Broker #{bid}",
                "Broker company (everyone)", ("Broker company", str(bid)))

    add("system", "Kavachio", "Automatic steps", ("zzz", "Kavachio"))
    actors.sort(key=lambda a: a.pop("_sort"))

    return {"actors": actors,
            "action_groups": [{"key": k, "label": lbl, "actions": list(acts)}
                              for k, lbl, acts in groups_for_seat(v.seat)],
            "seat": v.seat, "categories": list(CATEGORIES)}


def parse_actor(keys: Iterable[str]) -> tuple[tuple[int, ...], tuple[int, ...], bool]:
    """`user:12` / `broker:5` / `system` → (user ids, broker ids, system)."""
    uids, bids, system = [], [], False
    for key in keys:
        key = (key or "").strip()
        if not key:
            continue
        if key == "system":
            system = True
        elif key.startswith("user:") and key[5:].isdigit():
            uids.append(int(key[5:]))
        elif key.startswith("broker:") and key[7:].isdigit():
            bids.append(int(key[7:]))
    return tuple(uids), tuple(bids), system


def actions_for_group(group: str) -> tuple[str, ...]:
    """The action names behind one Action Type choice. An unknown value is
    treated as a single action name, so a link can deep-link one event type
    (?action=exception_decided) without it having to be a group."""
    if not group or group == "all":
        return ()
    for key, _label, actions, _seats in ACTION_GROUPS:
        if key == group:
            return tuple(actions)
    return (group,)


def categories_for_group(group: str) -> tuple[str, ...]:
    """Which stores a group can possibly live in — so choosing "Sign-in and
    security" does not also count every activity row for the total."""
    if group in ("auth", "sessions"):
        return ("auth",)
    if group == "downloads":
        return ("access",)
    # Exceptions live in exception_decision_log now, not activity_events —
    # except the review link's own rows (code asked for, signed in, checked,
    # submitted), which are the broker working through them.
    if group == "exceptions":
        return ("decision", "activity")
    if not group or group == "all":
        return CATEGORIES
    return ("activity",)


def window(days: Optional[int], since: Optional[str], until: Optional[str]
           ) -> tuple[Optional[datetime], Optional[datetime]]:
    """The date range, as naive UTC datetimes — which is what the columns hold.

    The browser sends an explicit UTC instant for each end (it knows the
    viewer's timezone; the server does not), so a "last 30 days" asked for at
    9am in Pune means the same 30 days here.
    """
    def parse(text: Optional[str]) -> Optional[datetime]:
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt.replace(tzinfo=None) - (dt.utcoffset() or timedelta(0)) \
            if dt.tzinfo else dt

    lo, hi = parse(since), parse(until)
    if lo is None and days:
        lo = datetime.utcnow() - timedelta(days=int(days))
    return lo, hi
