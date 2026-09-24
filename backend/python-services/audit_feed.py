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

MASKING
───────
A carrier deals with the broker COMPANY and never with the broker's own people
— the same rule decision_log.Labeller already applies to exception decisions.
So a row written by any broker seat reads, to a carrier viewer, as the broker
company under the role "Broker Admin". The broker's own seats see the person;
so does Kavachio. The masking happens on the way OUT, so the underlying row
keeps the truth and the broker admin can still hold their own team to account.

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

from sqlalchemy import desc, or_

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
    "carrier_admin":  "Carrier Admin",
    "carrier_user":   "Carrier User",
    "broker_admin":   "Broker Admin",
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
    "bdx_uploaded":              "Uploaded a bordereau",
    "direct_setup_uploaded":     "Uploaded a sample file",
    "supplement_uploaded":       "Uploaded a supporting file",
    "contract_uploaded":         "Uploaded a contract",
    "output_generated":          "Generated the output BDX",
    "direct_output_generated":   "Generated the output BDX",
    "direct_output_checked":     "Checked a file before sending",
    "datamodel.ingest":          "Loaded data into the data model",
    "validation.run":            "Ran the checks",
    # exceptions
    "exception_decided":         "Resolved exceptions",
    # setup
    "bordereau_setup_completed": "Completed a Bordereau Setup",
    "bordereau_setup_activated": "Activated a Bordereau Setup",
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
    "contract.rules_generated":  "Generated the contract rules",
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
    "contract_raised":            "Raised a contract",
    "contract_edited":            "Edited a contract",
    "contract_submitted":         "Submitted a contract",
    "contract_sent_for_review":   "Sent a contract for review",
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
    "signature_round_started":    "Sent a contract for signature",
    "signature_envelope_created": "Prepared a signing envelope",
    "signature_envelope_sent":    "Sent a signing envelope",
    "signing_code_resent":        "Resent their signing code",
    "contract_signature_declined": "Declined to sign a contract",
    "signature_reminder_sent":    "Reminded a signer",
    "signature_round_voided":     "Voided a signing round",
    "contract_endorsed":          "Endorsed a contract",
    "contract_checks_bound":      "Bound the contract's checks",
    "contract_mapped_to_template": "Mapped a contract to a template",
    "contract_review_skipped":    "Skipped the contract review",
    "contract_signed_from_email": "Signed a contract from the emailed link",
    "signing_code_verified":      "Verified their signing code",
    # the broker mesh
    "broker_added":               "Added a broker",
    "broker_linked":              "Linked an existing broker",
    "broker_invitation_declined": "Declined their invitation",
    "broker_invitation_withdrawn": "Withdrew an invitation",
    "broker_removed_from_programme": "Removed a broker from a programme",
    "broker_user_created":        "Created a broker user",
    "broker_invitation_accepted": "Accepted their invitation",
    "broker_put_on_programme":    "Put a broker on a programme",
    # the run
    "bordereau_run":              "Ran a bordereau",
    "bordereau_setup_created":    "Created a Bordereau Setup",
    "missing_columns_analyzed":   "Analysed the missing columns",
    "output_template_fields_saved": "Saved the output template fields",
    "output_template_from_standard": "Created a template from a standard",
    "output_sources_analyzed":    "Analysed the source files",
    "intake_route_created":       "Created a file route",
    "intake_key_created":         "Issued a file-route key",
    "mailbox_polled":             "Checked the mailbox for new files",
    "file_arrival_released":      "Released a held file",
    "onboarding_skipped":         "Skipped onboarding",
    # access_log — reads and downloads of output / source data
    "download":         "Downloaded a file",
    "read":             "Opened a record",
    "export":           "Exported data",
    # reminders written by the calendar sweep (actor: the system)
    "submission_overdue":   "Submission overdue",
    "submission_due_soon":  "Submission due soon",
    "submission_due_today": "Submission due today",
    "submission_chased":    "Chased a submission",
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
    if " " in action:
        method, _, path = action.partition(" ")
        try:
            from audit import _FRIENDLY, normalize_path
            named = _FRIENDLY.get((method, normalize_path(path)))
        except Exception:  # noqa: BLE001 — wording must never break a read
            named = None
        if named:
            return ACTION_WORDS.get(named, _humanize(named))
    return _humanize(action)


def _status_for(category: str, action: str, details: Any, ok: Any = None) -> tuple[str, str]:
    """(badge text, tone). Tone is one of ok | warn | info | bad | muted."""
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
            return "Not checked", "muted"
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
DECISION_WORDS = {
    "fix":     "Corrected a value",
    "approve": "Approved an exception",
    "dismiss": "Dismissed an exception",
    "reject":  "Rejected an exception",
}
DECISION_STATUS = {
    "fix":     ("Corrected", "warn"),
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
        return _clip(reason, 120) if reason else ""
    parts = []
    for key, value in d.items():
        if key in _DETAIL_SKIP or value is None or value == "" or value == []:
            continue
        if isinstance(value, (list, tuple, set)):
            value = ", ".join(str(x) for x in sorted(value) if x)
            if not value:
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
    def name(self, *, user_id=None, actor: Optional[str] = None,
             broker_party_id=None, role: Optional[str] = None) -> dict:
        """{name, role, role_key, org} for one row, as this viewer may see it."""
        u = self.user(user_id) or self.user_by_email(actor)

        # A `broker:<id>` actor with no seat recorded — a row written before
        # actor_user_id existed. The company is all it ever knew.
        if u is None and isinstance(actor, str) and actor.startswith("broker:"):
            m = _ID.match(actor)
            pid = int(m.group(1)) if m else None
            org = self.party_name(pid) or "Broker"
            return {"name": org, "role": ROLE_WORDS["broker_admin"],
                    "role_key": "broker_admin", "org": org}

        if u is None:
            # Nobody signed in: the calendar sweep, the email poller, an
            # unauthenticated intake. The mock calls this "System / Automation".
            if actor and "@" not in actor:
                return {"name": actor, "role": "Automation",
                        "role_key": "system", "org": None}
            return {"name": actor or "System", "role": "Automation",
                    "role_key": "system", "org": None}

        r = normalize_role(role or u.role)
        person = u.full_name or u.email

        if r in BROKER_ROLES:
            org = self.party_name(u.broker_party_id or broker_party_id)
            # The carrier deals with the company, never with its people — and
            # whatever a broker USER did reads as the broker admin's doing.
            if self.v.is_carrier:
                return {"name": org or "Broker", "role": ROLE_WORDS["broker_admin"],
                        "role_key": "broker_admin", "org": org}
            # Their own organisation, or Kavachio: the person.
            return {"name": person, "role": ROLE_WORDS[r], "role_key": r, "org": org}

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
)


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
      "bordereau_run"), ALL_SEATS),
    ("exceptions", "Exceptions", ("exception_decided",), ALL_SEATS),
    # Building a Bordereau Setup, a mapping or an output template is the
    # carrier's work. A broker runs against what the carrier built and never
    # touches one, so offering them this filter offers a guaranteed blank page.
    ("setup", "Setup and templates",
     ("bordereau_setup_completed", "bordereau_setup_activated",
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
      "contract_raised", "contract_edited", "contract_submitted",
      "contract_sent_for_review", "contract_terms_accepted",
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
      "broker_invitation_withdrawn", "broker_removed_from_programme",
      "program_created", "program_updated", "party_created", "party_updated",
      "party_contact_added", "party_contact_removed",
      "submission_schedule_updated", "submission_chased"), CARRIER_SEATS),
    # How files reach the CARRIER — mailboxes, routes and keys. The broker
    # sends; it is the carrier that sets up the ways in.
    ("intake", "How files arrive",
     ("intake_route_created", "intake_key_created", "mailbox_polled",
      "file_arrival_released"), CARRIER_SEATS),
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
        ors.append(model.actor.in_(_emails_of(f.actor_user_ids)))
    if f.actor_broker_ids:
        broker_col = getattr(model, "actor_broker_party_id", None)
        if broker_col is not None:
            ors.append(broker_col.in_(f.actor_broker_ids))
        ors.append(model.actor.in_([f"broker:{b}" for b in f.actor_broker_ids]))
        ors.append(_in_broker_users(model, f.actor_broker_ids))
    if f.actor_system:
        ors.append(or_(model.actor.is_(None), ~model.actor.like("%@%")))
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


def _decision_query(s, sc: Scope, f: Filters, q_ids):
    """Every exception decision in scope — ONE ROW PER DECISION, not per sweep.

    These were grouped per person / per file / per day, which read as "Resolved
    73 exceptions" and recorded nothing anybody could check: not which policy,
    not which field, not what the value became. An audit trail that cannot
    answer "what exactly did they change" is a counter, not a trail. The
    decision log holds every one of those, so each is its own line.
    """
    clause = _decision_scope_clause(sc)
    if clause is False:
        return None
    if f.actor_system:
        # Nobody signed in never decides an exception — a person always does.
        return None
    q = s.query(ExceptionDecisionLog)
    if clause is not None:
        q = q.filter(clause)
    if f.since is not None:
        q = q.filter(ExceptionDecisionLog.decided_at >= f.since)
    if f.until is not None:
        q = q.filter(ExceptionDecisionLog.decided_at <= f.until)
    if f.actor_user_ids:
        q = q.filter(ExceptionDecisionLog.decided_by_user_id.in_(f.actor_user_ids))
    if f.actor_broker_ids:
        q = q.filter(ExceptionDecisionLog.decided_by_broker_party_id.in_(f.actor_broker_ids))
    if f.q:
        uids, _bids = q_ids
        like = f"%{f.q}%"
        ors = [ExceptionDecisionLog.policy_number.ilike(like),
               ExceptionDecisionLog.field.ilike(like),
               ExceptionDecisionLog.sheet.ilike(like),
               ExceptionDecisionLog.kind.ilike(like),
               ExceptionDecisionLog.old_value.ilike(like),
               ExceptionDecisionLog.new_value.ilike(like)]
        if uids:
            ors.append(ExceptionDecisionLog.decided_by_user_id.in_(uids))
        q = q.filter(or_(*ors))
    return q


def _decision_rows(s, sc: Scope, f: Filters, q_ids, limit: int) -> tuple[list[dict], int]:
    q = _decision_query(s, sc, f, q_ids)
    if q is None:
        return [], 0
    total = q.count()
    out = []
    for r in (q.order_by(desc(ExceptionDecisionLog.decided_at),
                         desc(ExceptionDecisionLog.id)).limit(limit)):
        target = (f"Output BDX #{r.export_id}" if r.export_id
                  else f"Uploaded file #{r.landing_id}" if r.landing_id else "—")
        out.append({
            "kind": "decision", "id": r.id, "at": r.decided_at,
            "action": "exception_decided", "target": target,
            "details": None, "detail": _decision_detail(r),
            "decision_kind": (r.kind or "").lower(),
            "actor": None, "user_id": r.decided_by_user_id,
            "role": r.decided_by_role,
            "broker_party_id": r.decided_by_broker_party_id,
            "tenant_id": r.tenant_id, "ip": None, "ok": None,
        })
    return out, total


def _rows(s, sc: Scope, f: Filters, limit: int) -> tuple[list[dict], int]:
    """The newest `limit` rows across every selected store, plus the true total."""
    q_ids = _search_targets(s, f.q) if f.q else ([], [])
    out: list[dict] = []
    total = 0

    if "decision" in f.categories:
        d_rows, d_total = _decision_rows(s, sc, f, q_ids, limit)
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
            if not f.actions:
                q = q.filter(ActivityEvent.action.notin_(SUPPRESSED_ACTIONS))
            total += q.count()
            for e in q.order_by(desc(ActivityEvent.created_at), desc(ActivityEvent.id)).limit(limit):
                out.append({
                    "kind": "activity", "id": e.id, "at": e.created_at,
                    "action": e.action, "target": e.target, "details": e.details,
                    "detail": _detail_words(e.details),
                    "actor": e.actor, "user_id": getattr(e, "actor_user_id", None),
                    "role": getattr(e, "actor_role", None),
                    "broker_party_id": getattr(e, "actor_broker_party_id", None),
                    "tenant_id": e.tenant_id, "ip": (e.details or {}).get("ip")
                    if isinstance(e.details, dict) else None, "ok": None,
                })

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


def render(s, namer: ActorNamer, raw: dict) -> dict:
    """One stored row, as the screen shows it."""
    who = namer.name(user_id=raw["user_id"], actor=raw["actor"],
                     broker_party_id=raw["broker_party_id"], role=raw["role"])
    status, tone = _status_for(raw["kind"], raw["action"], raw["details"], raw["ok"])
    label = action_words(raw["action"])
    if raw["kind"] == "decision":
        kind = raw.get("decision_kind") or "fix"
        label = DECISION_WORDS.get(kind, "Decided an exception")
        status, tone = DECISION_STATUS.get(kind, ("Exception resolved", "warn"))
    return {
        "id": f"{raw['kind']}:{raw['id']}",
        "at": _iso(raw["at"]),
        "category": raw["kind"],
        "actor": who["name"],
        "actor_role": who["role"],
        "actor_role_key": who["role_key"],
        "actor_org": who["org"],
        "action": raw["action"],
        "action_label": label,
        "detail": raw.get("detail") or "",
        "target": _target_words(raw["target"], raw["details"]),
        "status": status,
        "tone": tone,
        "ip": raw["ip"],
        "carrier": namer.tenant_name(raw["tenant_id"]) if raw["tenant_id"] else None,
    }


def page(s, v: Viewer, f: Filters, page_no: int, page_size: int) -> dict:
    """One page of the feed, newest first, already in the viewer's words."""
    page_no = max(1, page_no)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    offset = min((page_no - 1) * page_size, MAX_OFFSET)
    sc = scope_for(s, v)
    raw, total = _rows(s, sc, f, offset + page_size)
    namer = ActorNamer(s, v)
    items = [render(s, namer, r) for r in raw[offset:offset + page_size]]
    return {"items": items, "total": total, "page": page_no, "page_size": page_size,
            "seat": v.seat}


def export_rows(s, v: Viewer, f: Filters) -> list[dict]:
    """Every row the current filters select, capped, for the download."""
    sc = scope_for(s, v)
    raw, _total = _rows(s, sc, f, MAX_EXPORT_ROWS)
    namer = ActorNamer(s, v)
    return [render(s, namer, r) for r in raw[:MAX_EXPORT_ROWS]]


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
        # A carrier viewer sees broker people as their company: one entry per
        # company, not one per person, or the dropdown would count the people
        # it is meant to hide.
        key = (f"broker:{u.broker_party_id}"
               if (r in BROKER_ROLES and v.is_carrier and u.broker_party_id)
               else f"user:{u.id}")
        add(key, who["name"], who["role"], (who["role"], who["name"]))

    # Every broker in reach, even one whose people have never acted — a carrier
    # looking for "what has this broker been doing" should find the broker and
    # be told "nothing yet", not be unable to ask.
    if v.is_carrier:
        for bid in sorted(sc.broker_party_ids):
            add(f"broker:{bid}", namer.party_name(bid) or f"Broker #{bid}",
                ROLE_WORDS["broker_admin"], (ROLE_WORDS["broker_admin"], str(bid)))

    add("system", "System", "Automation", ("zzz", "System"))
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
    # Exceptions live in exception_decision_log now, not activity_events.
    if group == "exceptions":
        return ("decision",)
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
