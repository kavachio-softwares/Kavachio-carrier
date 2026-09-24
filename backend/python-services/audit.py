"""Centralized DB audit-log writers + HTTP-audit helpers.

Three durable audit stores (all in the app Postgres DB — NOT operational/console
logs, which stay in stdout):

  * activity_events  — user-initiated mutations (via the request-audit middleware
                       in main.py, and the legacy _log() named events)
  * auth_audit       — security events (login/logout/reset/refresh)
  * access_log       — reads/downloads of sensitive output & source data

Every writer opens its own short-lived session and swallows errors: auditing must
never break or slow down the request it is recording.
"""
from __future__ import annotations


import logging
import re

from db import SessionLocal, ActivityEvent, AuthAudit, AccessLog, AppUser

log = logging.getLogger("bdx.audit")

# ---------------------------------------------------------------------------
# actor resolution
# ---------------------------------------------------------------------------

def actor_from_token(authorization: str | None) -> tuple[int | None, int | None]:
    """(user_id, tenant_id) from a 'Bearer <jwt>' header, or (None, None).
    Never raises — an anonymous / bad-token request just audits with no actor."""
    if not authorization or not authorization.startswith("Bearer "):
        return None, None
    try:
        from auth_tokens import decode_access_token
        claims = decode_access_token(authorization[7:].strip())
        return int(claims["sub"]), claims.get("tenant_id")
    except Exception:
        return None, None


# user_id -> (email, normalized role, broker_party_id). One lookup per user per
# process: the middleware needs all three on EVERY mutating request, and they
# change about as often as a person changes job.
_actor_cache: dict[int, tuple[str | None, str | None, int | None]] = {}


def actor_identity(user_id: int | None) -> tuple[str | None, str | None, int | None]:
    """(email, role, broker_party_id) for a user id. Best-effort, cached.

    This is what lets an audit row name the SEAT that acted, not only a display
    string — see the note on db.ActivityEvent. Never raises.
    """
    if user_id is None:
        return None, None, None
    hit = _actor_cache.get(user_id)
    if hit is not None:
        return hit
    ident: tuple[str | None, str | None, int | None] = (None, None, None)
    try:
        from auth_deps import normalize_role
        with SessionLocal() as s:
            u = s.get(AppUser, user_id)
            if u is not None:
                ident = (u.email, normalize_role(u.role), u.broker_party_id)
    except Exception as e:  # noqa: BLE001
        log.warning("audit: actor lookup failed for %s: %s", user_id, e)
    _actor_cache[user_id] = ident
    return ident


def forget_actor(user_id: int | None) -> None:
    """Drop a cached identity — call after a role / broker change so the next
    audit row records the new seat instead of the one held at first sight."""
    _actor_cache.pop(user_id, None)


def actor_email(user_id: int | None) -> str | None:
    """Resolve a user_id to its email (cached). Best-effort."""
    return actor_identity(user_id)[0]

def actor_columns(principal=None, user_id: int | None = None) -> dict:
    """The three ``actor_*`` values for an ActivityEvent built by hand.

    ``s.add(ActivityEvent(..., **actor_columns(principal)))`` records the acting
    seat on a row that does not go through log_activity(). Never raises.
    """
    uid = user_id if user_id is not None else getattr(principal, "user_id", None)
    _email, role, broker_id = actor_identity(uid)
    return {"actor_user_id": uid, "actor_role": role,
            "actor_broker_party_id": broker_id}


def actor_for(principal) -> str | None:
    """The actor to record for ``principal`` on rows the CARRIER reads.

    A broker seat is recorded as its broker company, ``broker:<party id>`` —
    the same label a run submitted through the broker's lane carries. The
    carrier deals with the broker company and never sees the broker's own
    users, so their emails do not belong in the carrier's activity. Everyone
    else is recorded by email, as before. Best-effort, like actor_email.
    """
    if principal is None:
        return None
    if getattr(principal, "is_broker", False):
        try:
            from auth_deps import resolve_broker_party_id
            with SessionLocal() as s:
                bid = resolve_broker_party_id(s, principal)
            if bid is not None:
                return f"broker:{bid}"
        except Exception as e:  # noqa: BLE001
            log.warning("audit: broker actor lookup failed for %s: %s",
                        principal.user_id, e)
    return actor_email(principal.user_id)

# ---------------------------------------------------------------------------
# writers
# ---------------------------------------------------------------------------

def log_activity(tenant_id, actor, action, target=None, details=None, *,
                 actor_user_id=None, principal=None) -> None:
    """Append one activity row.

    `actor` stays the display string every existing caller already passes. Give
    EITHER `principal` (preferred — the request's Principal) or `actor_user_id`
    and the acting seat is recorded alongside it, which is what makes the row
    reachable by the Audit Logs screen's role scoping. Omit both and the row is
    written exactly as before, and is resolved from `actor` at read time.
    """
    uid = actor_user_id if actor_user_id is not None else getattr(principal, "user_id", None)
    _email, role, broker_id = actor_identity(uid)
    try:
        with SessionLocal() as s:
            s.add(ActivityEvent(tenant_id=tenant_id, actor=actor, action=action,
                                target=target, details=details or {},
                                actor_user_id=uid, actor_role=role,
                                actor_broker_party_id=broker_id))
            s.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("audit: log_activity failed (%s): %s", action, e)


def log_auth(event, actor=None, ok=None, ip=None, user_agent=None,
             user_id=None, tenant_id=None, details=None) -> None:
    try:
        with SessionLocal() as s:
            s.add(AuthAudit(user_id=user_id, tenant_id=tenant_id, actor=actor,
                            event=event, ok=ok, ip=ip, user_agent=user_agent,
                            details=details or {}))
            s.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("audit: log_auth failed (%s): %s", event, e)


def log_access(actor, resource, action, ip=None, user_id=None, tenant_id=None) -> None:
    try:
        with SessionLocal() as s:
            s.add(AccessLog(tenant_id=tenant_id, user_id=user_id, actor=actor,
                            resource=resource, action=action, ip=ip))
            s.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("audit: log_access failed (%s %s): %s", action, resource, e)

# ---------------------------------------------------------------------------
# HTTP path helpers (used by the middleware)
# ---------------------------------------------------------------------------

_NUM = re.compile(r"/\d+")

# Path segments that are SECRETS, not ids. A signing link is a bearer
# credential: anyone holding it can sign that contract. It was being written
# into the audit row twice — as the action name and as the target — which put a
# live signing link in a log that carriers, brokers and Kavachio staff all read.
# Redacted here, at the only place a request path becomes an audit row.
_SECRET_SEGMENTS = [
    (re.compile(r"(/esign/sign/)[^/]+"), r"\1{token}"),
]

# Id segments that are not NUMBERS. A tenant is addressed by its code, so
# /tenants/acme/transfer-ownership and /tenants/globex/transfer-ownership were
# two different "actions" — 28 one-off rows for what is one event, and a filter
# entry per carrier. Collapsed like any other id.
_NAMED_IDS = [
    (re.compile(r"(/tenants/)(?!new$|new/)[^/]+"), r"\1{code}"),
]


def redact_path(path: str) -> str:
    """Blank out secret-bearing segments, leaving ids alone.

    Kept apart from normalize_path because the two are wanted separately: a
    reader naming "/export/downloads/37/file" needs the 37, and must never need
    the signing token."""
    for rx, repl in _SECRET_SEGMENTS:
        path = rx.sub(repl, path)
    return path


def normalize_path(path: str) -> str:
    """Collapse id segments so actions group: /direct/format/211 ->
    /direct/format/{id}. Secrets are redacted first, never collapsed."""
    path = redact_path(path)
    for rx, repl in _NAMED_IDS:
        path = rx.sub(repl, path)
    return _NUM.sub("/{id}", path)


# Business-meaningful action names for mutating endpoints that write a domain
# record but emit no named event of their own. The middleware logs these instead
# of a bare "POST /path", so the audit feed reads as business events (with the
# real actor and the id-bearing path as `target`, e.g. /export/downloads/355/decide).
# Keyed by (method, normalized-path).
_FRIENDLY = {
    ("POST",   "/export/downloads/{id}/decide"):     "exception_decided",
    ("POST",   "/api/validate/exceptions/decide"):   "exception_decided",
    ("POST",   "/parties/{id}/contacts"):            "party_contact_added",
    ("DELETE", "/parties/{id}/contacts/{id}"):       "party_contact_removed",
    ("POST",   "/programs/{id}/contracts"):          "contract_uploaded",
    ("POST",   "/programs/{id}/setup"):              "bordereau_setup_completed",
    ("POST",   "/pipelines/{id}/activate"):          "bordereau_setup_activated",
    ("PUT",    "/mappers/{id}/sheet-bindings"):      "sheet_bindings_saved",
    ("PUT",    "/users/{id}"):                       "user_updated",
    ("DELETE", "/users/{id}"):                       "user_deleted",
    ("POST",   "/direct/upload"):                    "direct_setup_uploaded",
    ("PUT",    "/direct/format/{id}"):               "bdx_setup_updated",
    ("DELETE", "/direct/format/{id}"):               "bdx_setup_deleted",
    ("POST",   "/direct/format/{id}/supplement"):    "supplement_uploaded",
    ("POST",   "/admin/mapping-tasks/{id}/propose"): "datamodel_mapping_proposed",
    ("POST",   "/admin/mapping-tasks/{id}/resolve"): "mapping_task_resolved",
    ("POST",   "/mapper/generate"):                  "input_mapper_generated",
    ("PUT",    "/mapper/{id}"):                       "input_mapper_updated",
    ("POST",   "/mapper/{id}/activate"):             "input_mapper_activated",
    ("POST",   "/bdx/upload"):                       "bdx_uploaded",
    ("POST",   "/export/template/generate"):         "output_template_generated",
    ("PUT",    "/export/template/{id}"):             "output_template_updated",
    ("POST",   "/export/template/{id}/activate"):    "output_template_activated",
    ("POST",   "/export/template/{id}/refresh"):     "output_template_refreshed",
    ("POST",   "/api/canonical/fields/save"):        "canonical_fields_saved",
    # --- the contract's life, start to finish. These were the single biggest
    # gap in the trail: every one of them WAS recorded, but as "POST
    # /contracts/{id}/accept-terms", which reads as a server log rather than as
    # the business event it is.
    ("POST",   "/contracts"):                        "contract_raised",
    ("PATCH",  "/contracts/{id}"):                   "contract_edited",
    ("POST",   "/contracts/{id}/submit"):            "contract_submitted",
    ("POST",   "/contracts/{id}/send-for-review"):   "contract_sent_for_review",
    ("POST",   "/contracts/{id}/accept-terms"):      "contract_terms_accepted",
    ("POST",   "/contracts/{id}/request-changes"):   "contract_changes_requested",
    ("POST",   "/contracts/{id}/approve"):           "contract_approved",
    ("POST",   "/contracts/{id}/reject"):            "contract_rejected",
    ("POST",   "/contracts/{id}/activate"):          "contract_activated",
    ("POST",   "/contracts/{id}/terminate"):         "contract_terminated",
    ("POST",   "/contracts/{id}/renew"):             "contract_renewed",
    ("POST",   "/contracts/{id}/sign"):              "contract_signed",
    ("POST",   "/contracts/{id}/submit-signed"):     "contract_signed_copy_submitted",
    ("POST",   "/contracts/{id}/documents"):         "contract_document_attached",
    ("POST",   "/contract-wording/pages"):           "contract_wording_saved",
    ("POST",   "/contract-wording/pages/{id}"):      "contract_wording_saved",
    ("POST",   "/contract-wording/draft"):           "contract_wording_drafted",
    # --- signing
    ("POST",   "/esign/contracts/{id}/signing-session"): "signature_round_started",
    ("POST",   "/esign/sign/{token}"):               "contract_signed_from_email",
    ("POST",   "/esign/sign/{token}/verify"):        "signing_code_verified",
    # --- the broker mesh. A few keys below name routes that no longer exist
    # (/contracts/{id}/approve, /submit, /reject, /brokers/link): they are what
    # the ROWS ALREADY IN THE TABLE say, and this map is read on the way out as
    # well as on the way in, so keeping them is what makes that history legible.
    ("POST",   "/brokers"):                          "broker_added",
    ("POST",   "/broker/users"):                     "broker_user_created",
    ("POST",   "/broker/invitations/{id}/accept"):   "broker_invitation_accepted",
    ("POST",   "/programs/{id}/brokers"):            "broker_put_on_programme",
    # --- the run itself, through the canonical carrier-scoped path. This is
    # the broker's whole reason for having a login, and it had no name.
    ("POST",   "/carriers/{id}/programs/{id}/brokers/{id}/contracts/{id}/runs"):
        "bordereau_run",
    ("POST",   "/pipelines"):                        "bordereau_setup_created",
    ("POST",   "/pipelines/{id}/missing-columns/analyze"): "missing_columns_analyzed",
    ("PUT",    "/pipelines/{id}"):                   "bdx_setup_updated",
    # --- invitations
    ("POST",   "/broker/invitations/{id}/decline"):  "broker_invitation_declined",
    ("POST",   "/broker-invitations/{id}/resend"):   "invite_resent",
    ("DELETE", "/broker-invitations/{id}"):          "broker_invitation_withdrawn",
    ("POST",   "/brokers/link"):                     "broker_linked",
    ("DELETE", "/programs/{id}/brokers/{id}"):       "broker_removed_from_programme",
    # --- contracts, the remaining verbs
    ("POST",   "/programs/{id}/contracts/{id}/generate-rules"): "contract.rules_generated",
    ("POST",   "/contracts/{id}/endorsement"):       "contract_endorsed",
    ("POST",   "/contracts/{id}/bind-checks"):       "contract_checks_bound",
    ("POST",   "/contracts/{id}/map-to-template"):   "contract_mapped_to_template",
    ("POST",   "/contracts/{id}/skip-review"):       "contract_review_skipped",
    # --- signing, the remaining verbs
    ("POST",   "/esign/envelopes"):                  "signature_envelope_created",
    ("POST",   "/esign/envelopes/{id}/send"):        "signature_envelope_sent",
    ("POST",   "/esign/sign/{token}/resend-code"):   "signing_code_resent",
    ("POST",   "/esign/envelopes/{id}/remind"):       "signature_reminder_sent",
    ("POST",   "/esign/envelopes/{id}/void"):         "signature_round_voided",
    ("POST",   "/esign/sign/{token}/decline"):       "contract_signature_declined",
    # --- output templates
    ("POST",   "/output-template/{id}/fields"):      "output_template_fields_saved",
    ("PUT",    "/output-template/{id}/fields"):      "output_template_fields_saved",
    ("POST",   "/output-template/from-standard"):    "output_template_from_standard",
    ("POST",   "/output-template/analyze-sources"):  "output_sources_analyzed",
    # --- how files reach the carrier
    ("POST",   "/intake/routes"):                    "intake_route_created",
    ("POST",   "/intake/routes/{id}/keys"):          "intake_key_created",
    ("POST",   "/intake/routes/{id}/poll"):          "mailbox_polled",
    ("POST",   "/intake/arrivals/{id}/release"):     "file_arrival_released",
    ("POST",   "/v1/bordereaux"):                    "bdx_uploaded",
    # --- the rest
    ("POST",   "/calendar/chase"):                   "submission_chased",
    ("PUT",    "/programs/{id}/schedule"):           "submission_schedule_updated",
    ("POST",   "/tenants/{code}/transfer-ownership"): "ownership_transferred",
    ("POST",   "/onboarding/skip"):                  "onboarding_skipped",
}

def friendly_action(method: str, path: str) -> str:
    """A business action name for a mutating request, else 'METHOD /path/{id}'."""
    norm = normalize_path(path)
    return _FRIENDLY.get((method, norm), f"{method} {norm}")


# Mutations that already write a rich, named event via app_routes._log(); the
# middleware SKIPS these so we don't double-log. Everything else that mutates is
# captured generically by the middleware. (/auth/* is skipped separately and
# handled by auth_audit.)
_SELF_LOGGED = [
    ("POST",   re.compile(r"^/extra-fields$")),
    ("POST",   re.compile(r"^/extra-fields/[^/]+/adopt$")),
    ("POST",   re.compile(r"^/tenants$")),
    ("PUT",    re.compile(r"^/tenants/[^/]+$")),
    ("POST",   re.compile(r"^/parties$")),
    ("PUT",    re.compile(r"^/parties/\d+$")),
    ("POST",   re.compile(r"^/programs$")),
    ("PUT",    re.compile(r"^/programs/\d+$")),
    ("PUT",    re.compile(r"^/programs/\d+/contracts/\d+/rules/\d+/output-field$")),
    ("POST",   re.compile(r"^/programs/\d+/contracts/\d+/rules/\d+/variation-values$")),
    ("POST",   re.compile(r"^/programs/\d+/contracts/\d+/rules/\d+/variation-values/remove$")),
    ("POST",   re.compile(r"^/programs/\d+/contracts/\d+/clause-routing/\d+/resolve$")),
    ("DELETE", re.compile(r"^/programs/\d+/contracts/\d+/rules/\d+$")),
    ("POST",   re.compile(r"^/programs/\d+/contracts/\d+/activate$")),
    ("POST",   re.compile(r"^/users$")),
    ("POST",   re.compile(r"^/users/\d+/resend-invite$")),
    ("PUT",    re.compile(r"^/users/\d+/profile$")),
    # These log a richer event (with counts) inside the endpoint, so the generic
    # middleware entry is skipped here to avoid double-logging.
    ("POST",   re.compile(r"^/api/validate$")),
    ("POST",   re.compile(r"^/export/template/\d+/build-rules$")),
    # These already emit a meaningful BUSINESS event (direct_output_generated /
    # output_generated / datamodel.ingest with filename, rows, exceptions,
    # loaded/failed counts), so skip the generic "POST /path" middleware row.
    ("POST",   re.compile(r"^/direct/run$")),
    ("POST",   re.compile(r"^/direct/render$")),
    ("POST",   re.compile(r"^/export/downloads/\d+/rerender$")),
    ("POST",   re.compile(r"^/export/generate$")),
    ("POST",   re.compile(r"^/direct/landing/\d+/load-datamodel$")),
    # Read/preview POSTs — they persist NOTHING (sheet peek, preview, dry-run
    # validate, field preview), so a "mutation" audit row is misleading noise.
    ("POST",   re.compile(r"^/direct/peek-sheets$")),
    ("POST",   re.compile(r"^/bdx/sheets$")),
    ("POST",   re.compile(r"^/bdx/preview$")),
    ("POST",   re.compile(r"^/export/validate$")),
    ("POST",   re.compile(r"^/api/canonical/field/preview$")),
    # Two more of the same kind, found when the Audit Logs screen made the feed
    # readable: they were 1,946 of 14,186 rows — 14% of the entire trail — and
    # neither persists anything. A contract wording preview is a POST only
    # because it posts the draft it is rendering.
    ("POST",   re.compile(r"^/contract-wording/preview$")),
    ("POST",   re.compile(r"^/contracts/\d+/endorsement/preview$")),
    # Marking your own notifications as read is bookkeeping on your own inbox.
    ("POST",   re.compile(r"^/admin/notifications/read$")),
    # Write endpoints that emit an explicit RICH event in their handler
    # (meaningful target + business details) — skip the generic middleware row so
    # there's exactly one meaningful audit row.
    ("POST",   re.compile(r"^/export/template/generate$")),
    ("PUT",    re.compile(r"^/export/template/\d+$")),
    ("POST",   re.compile(r"^/programs/\d+/contracts$")),
    ("POST",   re.compile(r"^/programs/\d+/setup$")),
    ("POST",   re.compile(r"^/parties/\d+/contacts$")),
    ("PUT",    re.compile(r"^/users/\d+$")),
    ("POST",   re.compile(r"^/mapper/generate$")),
    ("PUT",    re.compile(r"^/mapper/\d+$")),
    ("POST",   re.compile(r"^/bdx/upload$")),
    ("POST",   re.compile(r"^/direct/upload$")),
    ("POST",   re.compile(r"^/admin/mapping-tasks/\d+/resolve$")),
    ("POST",   re.compile(r"^/admin/mapping-tasks/\d+/propose$")),
    ("POST",   re.compile(r"^/export/downloads/\d+/decide$")),
    ("POST",   re.compile(r"^/api/validate/exceptions/decide$")),
    ("POST",   re.compile(r"^/api/canonical/fields/save$")),
    ("PUT",    re.compile(r"^/direct/format/\d+$")),
    ("DELETE", re.compile(r"^/direct/format/\d+$")),
    ("POST",   re.compile(r"^/direct/format/\d+/supplement$")),
    ("DELETE", re.compile(r"^/users/\d+$")),
    ("DELETE", re.compile(r"^/parties/\d+/contacts/\d+$")),
    ("POST",   re.compile(r"^/mapper/\d+/activate$")),
    ("POST",   re.compile(r"^/export/template/\d+/activate$")),
    ("POST",   re.compile(r"^/export/template/\d+/refresh$")),
    ("PUT",    re.compile(r"^/mappers/\d+/sheet-bindings$")),
]

def is_self_logged(method: str, path: str) -> bool:
    return any(m == method and rx.match(path) for m, rx in _SELF_LOGGED)


# GET endpoints that return sensitive output / source data → access_log.
# Curated on purpose: only data-bearing detail/download reads, NOT every list GET
# (which would flood the log with page-load noise).
_ACCESS_PATHS = [
    re.compile(r"^/export/downloads/\d+/file$"),
    re.compile(r"^/export/downloads/\d+/data$"),
    re.compile(r"^/export/downloads/\d+$"),
    re.compile(r"^/uploads/\d+/file$"),
    re.compile(r"^/uploads/\d+/source-rows$"),
    re.compile(r"^/uploads/\d+$"),
    re.compile(r"^/mapper/\d+/file$"),
    re.compile(r"^/dwh$"),
    # Taking the audit trail out of the building is itself worth a line in it.
    re.compile(r"^/audit/export$"),
]

def is_access_path(path: str) -> bool:
    return any(rx.match(path) for rx in _ACCESS_PATHS)
