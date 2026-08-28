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


_email_cache: dict[int, str | None] = {}

def actor_email(user_id: int | None) -> str | None:
    """Resolve a user_id to its email (cached). Best-effort."""
    if user_id is None:
        return None
    if user_id in _email_cache:
        return _email_cache[user_id]
    email = None
    try:
        with SessionLocal() as s:
            u = s.get(AppUser, user_id)
            email = u.email if u else None
    except Exception as e:  # noqa: BLE001
        log.warning("audit: actor_email lookup failed for %s: %s", user_id, e)
    _email_cache[user_id] = email
    return email

# ---------------------------------------------------------------------------
# writers
# ---------------------------------------------------------------------------

def log_activity(tenant_id, actor, action, target=None, details=None) -> None:
    try:
        with SessionLocal() as s:
            s.add(ActivityEvent(tenant_id=tenant_id, actor=actor, action=action,
                                target=target, details=details or {}))
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

def normalize_path(path: str) -> str:
    """Collapse numeric id segments so actions group: /direct/format/211 ->
    /direct/format/{id}."""
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
]

def is_access_path(path: str) -> bool:
    return any(rx.match(path) for rx in _ACCESS_PATHS)
