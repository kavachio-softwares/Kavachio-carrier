"""Platform-admin notifications — outbound email + the in-app feed.

Some things a broker does need Kavachio staff to know about. Today that is one
event (a Bordereau Setup being activated), but nothing in this module knows what
a setup IS: a caller supplies a `kind`, a headline, and a list of labelled facts,
and this module takes care of

  1. recording it in `platform_notification` (the feed every kavachio_admin
     reads on their next sign-in), and
  2. emailing every kavachio_admin.

Adding a second notifiable event therefore needs no schema change and no change
here — only a `notify_platform_admins(...)` call at the new site.

Two rules the whole module is built around:

  * **It can never break the action that triggered it.** Every public function
    swallows its own errors; a dead SMTP server or a locked table must not fail
    a user's activation.
  * **Recipients are resolved from data, never hardcoded and never configured.**
    They are exactly the ACTIVE holders of the kavachio_admin role in `app_user`
    — the equivalent of ``SELECT email FROM app_user WHERE role =
    'kavachio_admin' AND status = 'active'`` (legacy role spellings included,
    via auth_deps.db_role_values). Onboarding a new platform admin is a user
    record, and deactivating one stops their mail; there is no address list to
    keep in sync anywhere.

Mail goes out through email_utils.send_email on its OWN named sender account
(NOTIFY_SMTP_USER / NOTIFY_SMTP_PASS — an App Password), so platform notices
come from a Kavachio mailbox while password-reset and invite mail keeps using
the default sender. Anything the NOTIFY_* vars don't set falls back to the
default SMTP_* ones, so the two senders share host/port config.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from html import escape
from typing import Any, Optional, Sequence

from sqlalchemy import func

from auth_deps import db_role_values, normalize_role
from db import AppUser, PlatformNotification, SessionLocal

log = logging.getLogger("bdx.notify")

# The role that receives platform notifications. Everything below resolves
# recipients through this constant + auth_deps, so the role vocabulary lives in
# exactly one place.
PLATFORM_ADMIN_ROLE = "kavachio_admin"

# The role that receives a broker's OWN operational notices (C-9 submission
# deadlines). Scoped to one tenant — unlike PLATFORM_ADMIN_ROLE, which is
# cross-tenant Kavachio staff.
TENANT_ADMIN_ROLE = "tenant_admin"

# The named email_utils account these notices send as — i.e. the NOTIFY_SMTP_*
# environment variables. Falls back to the default SMTP_* sender when unset, so
# an environment that hasn't configured a separate mailbox still delivers.
NOTIFY_MAIL_ACCOUNT = "NOTIFY"


# ---------------------------------------------------------------------------
# recipients
# ---------------------------------------------------------------------------

def platform_admin_recipients() -> list[dict]:
    """Everyone who should receive a platform notification email — the live
    answer to ``SELECT email FROM app_user WHERE role = 'kavachio_admin' AND
    status = 'active'``.

    Returns ``[{"email": ..., "name": ...}, ...]``, de-duplicated case-
    insensitively. There is no configured address list: grant someone the role
    and they start receiving these; revoke it, or deactivate the account, and
    they stop.

    The status filter matters as much as the role one. `auth_login` refuses any
    account whose status isn't 'active', so mailing one would be sending work to
    somebody locked out of the app — and it makes deactivating an admin actually
    stop their notifications, which is what an operator expects it to do. NULL
    status counts as active, matching the column's own default.

    Never raises — an unreachable DB yields an empty list, which the caller logs
    and moves past rather than failing the action that triggered it."""
    people: list[dict] = []
    try:
        with SessionLocal() as s:
            rows = (s.query(AppUser.email, AppUser.full_name)
                    .filter(AppUser.role.in_(db_role_values(PLATFORM_ADMIN_ROLE)),
                            func.lower(func.coalesce(AppUser.status, "active"))
                            == "active")
                    .all())
        for email, full_name in rows:
            people.append({"email": (email or "").strip(),
                           "name": (full_name or "").strip() or None})
    except Exception as e:  # noqa: BLE001
        log.warning("notify: could not resolve platform admins from the DB: %s", e)
    return _dedupe_people(people)


def _dedupe_people(people: list[dict]) -> list[dict]:
    """Drop malformed and duplicate addresses, keeping first-seen order."""
    seen: set[str] = set()
    out: list[dict] = []
    for p in people:
        email = p["email"]
        # "@" is the whole validity bar here: these come from the admin-managed
        # user table, not from public input.
        if "@" not in email:
            continue
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def tenant_admin_recipients(tenant_id: Optional[int]) -> list[dict]:
    """The broker admins of ONE tenant — who a C-9 deadline digest goes to.

    Tenant-scoped twin of platform_admin_recipients(): same role+status rules,
    plus a tenant filter, so a deadline notice can never cross tenants.

    The status filter matters as much as the role one. `auth_login` refuses any
    account whose status isn't 'active', so mailing one would be sending work to
    somebody locked out of the app — and it makes deactivating an admin actually
    stop their notifications. NULL status counts as active, matching the default.

    Never raises: an unreachable DB yields an empty list, which the caller logs
    and moves past rather than failing the sweep that triggered it.
    """
    if tenant_id is None:
        return []
    people: list[dict] = []
    try:
        with SessionLocal() as s:
            rows = (s.query(AppUser.email, AppUser.full_name)
                    .filter(AppUser.tenant_id == tenant_id,
                            AppUser.role.in_(db_role_values(TENANT_ADMIN_ROLE)),
                            func.lower(func.coalesce(AppUser.status, "active"))
                            == "active")
                    .all())
        for email, full_name in rows:
            people.append({"email": (email or "").strip(),
                           "name": (full_name or "").strip() or None})
    except Exception as e:  # noqa: BLE001
        log.warning("notify: could not resolve tenant %s admins from the DB: %s",
                    tenant_id, e)
    return _dedupe_people(people)


# ---------------------------------------------------------------------------
# email body
# ---------------------------------------------------------------------------

# The "why am I getting this?" line at the bottom of every notification email.
# It varies by AUDIENCE, not by event: a broker admin told they are a "Kavachio
# platform administrator" will reasonably assume the mail was misrouted. Each
# sender passes the wording that matches who it resolved as recipients, which
# is why this is a parameter rather than a constant in the template.
PLATFORM_ADMIN_FOOTER = (
    "You're receiving this because you're a Kavachio platform administrator.")
TENANT_ADMIN_FOOTER = (
    "You're receiving this because you're an administrator on this Kavachio "
    "account. Submission deadlines are set per program under My Calendar.")


def notification_email_html(title: str, body: Optional[str],
                            facts: Sequence[tuple[str, Any]],
                            link: Optional[str] = None,
                            link_label: str = "Open Kavachio",
                            greeting_name: Optional[str] = None,
                            action: Optional[str] = None,
                            footer: str = PLATFORM_ADMIN_FOOTER) -> str:
    """Branded HTML body for a notification email.

    Renders whatever labelled facts the caller passes and, when given, the ONE
    action being asked for — it has no knowledge of any particular event type,
    which is what keeps a new notification kind from needing a new template.
    Shares the visual language of the reset/invite emails in email_utils.py.

    Built from tables and inline styles only: Outlook and several webmail
    clients strip <style> blocks and don't support flex/grid, so anything
    fancier would render as an unstyled stack of text for a chunk of readers."""
    greeting = f"Hi {escape(greeting_name)}," if (greeting_name or "").strip() else "Hi,"

    visible = [(label, value) for label, value in facts if value not in (None, "")]
    rows = "".join(
        f"""
          <tr>
            <td style="padding:11px 16px;font-size:12px;color:#8B93A2;font-weight:600;
                       letter-spacing:.4px;text-transform:uppercase;white-space:nowrap;
                       vertical-align:top;border-top:{'0' if i == 0 else '1px solid #EDEFF4'}">
              {escape(str(label))}</td>
            <td style="padding:11px 16px 11px 0;font-size:14px;color:#0E1320;font-weight:600;
                       vertical-align:top;border-top:{'0' if i == 0 else '1px solid #EDEFF4'}">
              {escape(str(value))}</td>
          </tr>"""
        for i, (label, value) in enumerate(visible)
    )
    facts_block = (
        f"""
      <table role="presentation" cellpadding="0" cellspacing="0" border="0"
             style="width:100%;border-collapse:separate;border-spacing:0;background:#FAFBFD;
                    border:1px solid #E5E8EE;border-radius:12px;margin:0 0 24px">
        {rows}
      </table>"""
        if rows else ""
    )
    body_block = (
        f"""<p style="font-size:14.5px;color:#566071;line-height:1.65;margin:0 0 22px">
              {escape(body)}</p>"""
        if (body or "").strip() else ""
    )
    # The single next step, called out so it survives skim-reading.
    action_block = (
        f"""
      <table role="presentation" cellpadding="0" cellspacing="0" border="0"
             style="width:100%;border-collapse:collapse;margin:0 0 24px">
        <tr>
          <td style="padding:14px 18px;background:#F1F4FE;border-left:3px solid #3149C6;
                     border-radius:0 10px 10px 0">
            <div style="font-size:11px;color:#3149C6;font-weight:700;letter-spacing:.7px;
                        text-transform:uppercase;margin-bottom:5px">What to do next</div>
            <div style="font-size:14.5px;color:#0E1320;font-weight:600;line-height:1.5">
              {escape(action)}</div>
          </td>
        </tr>
      </table>"""
        if (action or "").strip() else ""
    )
    button = (
        f"""
      <table role="presentation" cellpadding="0" cellspacing="0" border="0">
        <tr><td style="border-radius:9px;background:#3149C6">
          <a href="{escape(link)}" style="display:inline-block;color:#ffffff;
             text-decoration:none;font-size:14.5px;font-weight:600;padding:13px 26px;
             border-radius:9px">{escape(link_label)} &rarr;</a>
        </td></tr>
      </table>"""
        if (link or "").strip() else ""
    )
    return f"""\
<!doctype html><html><body style="margin:0;padding:0;background:#F3F4F7;
      font-family:Inter,'Segoe UI',Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;background:#F3F4F7">
    <tr><td align="center" style="padding:40px 20px">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0"
             style="width:100%;max-width:600px;background:#ffffff;border:1px solid #E5E8EE;
                    border-radius:16px;overflow:hidden;
                    box-shadow:0 10px 26px -10px rgba(14,19,32,.12)">
        <!-- brand accent -->
        <tr><td style="height:4px;background:#3149C6;line-height:4px;font-size:0">&nbsp;</td></tr>
        <tr><td style="padding:34px 40px 38px">
          <!-- header -->
          <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;margin-bottom:26px">
            <tr>
              <td style="vertical-align:middle">
                <div style="font-size:19px;font-weight:700;color:#0E1320;letter-spacing:-.2px">Kavachio</div>
                <div style="font-size:12px;color:#8B93A2;margin-top:3px">Bordereau validation &amp; reporting</div>
              </td>
              <td align="right" style="vertical-align:middle">
                <span style="display:inline-block;background:#FFF4E5;color:#B26A00;font-size:10.5px;
                             font-weight:700;letter-spacing:.7px;text-transform:uppercase;
                             padding:6px 11px;border-radius:20px;white-space:nowrap">Action needed</span>
              </td>
            </tr>
          </table>

          <h1 style="font-size:20px;line-height:1.35;color:#0E1320;margin:0 0 16px;
                     font-weight:700;letter-spacing:-.2px">{escape(title)}</h1>
          <p style="font-size:14.5px;color:#0E1320;line-height:1.6;margin:0 0 14px">{greeting}</p>
          {body_block}
          {facts_block}
          {action_block}
          {button}
        </td></tr>
        <tr><td style="padding:18px 40px 26px;border-top:1px solid #EDEFF4;background:#FAFBFD">
          <div style="font-size:12px;color:#8B93A2;line-height:1.6">
            {escape(footer)}
          </div>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


def _email_text(title: str, body: Optional[str],
                facts: Sequence[tuple[str, Any]], link: Optional[str],
                action: Optional[str] = None) -> str:
    """Plain-text alternative, same content as the HTML part."""
    lines = [title, ""]
    if (body or "").strip():
        lines += [body.strip(), ""]
    lines += [f"{label}: {value}" for label, value in facts if value not in (None, "")]
    if (action or "").strip():
        lines += ["", f"What to do next: {action.strip()}"]
    if (link or "").strip():
        lines += ["", link]
    return "\n".join(lines)


def _app_link(path: Optional[str]) -> Optional[str]:
    """Absolute app URL for a relative path, using the same APP_BASE_URL the
    reset/invite emails link to. Returns None when there's no path."""
    if not path:
        return None
    base = os.getenv("APP_BASE_URL", "http://localhost:5173").rstrip("/")
    return f"{base}/{path.lstrip('/')}"


# ---------------------------------------------------------------------------
# write path
# ---------------------------------------------------------------------------

def _send_emails(recipients: list[dict], title: str, body: Optional[str],
                 facts: Sequence[tuple[str, Any]], link: Optional[str],
                 link_label: str, subject: str,
                 action: Optional[str] = None) -> None:
    """Mail every recipient, one message each. Per-recipient failures are logged
    and skipped so one bad address can't stop the rest."""
    try:
        from email_utils import send_email
    except Exception as e:  # noqa: BLE001
        log.warning("notify: email transport unavailable (%s) — feed row still written", e)
        return
    text = _email_text(title, body, facts, link, action)
    for person in recipients:
        try:
            send_email(
                person["email"], subject,
                notification_email_html(title, body, facts, link, link_label,
                                        greeting_name=person.get("name"),
                                        action=action),
                text=text, account=NOTIFY_MAIL_ACCOUNT,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("notify: email to %s failed: %s", person["email"], e)


def notify_platform_admins(
    kind: str,
    title: str,
    *,
    body: Optional[str] = None,
    facts: Optional[Sequence[tuple[str, Any]]] = None,
    tenant_id: Optional[int] = None,
    actor: Optional[str] = None,
    target: Optional[str] = None,
    details: Optional[dict] = None,
    link_path: Optional[str] = None,
    link_label: str = "Open Kavachio",
    subject: Optional[str] = None,
    label: Optional[str] = None,
    label_plural: Optional[str] = None,
    action: Optional[str] = None,
    send_email_async: bool = True,
) -> Optional[int]:
    """Record a platform notification and email every kavachio_admin.

    `facts` is an ordered list of (label, value) pairs — the human-readable
    detail shown in both the email and the in-app feed. It is stored inside
    `details` so the UI renders exactly what was mailed, with no second source
    of truth to drift.

    `label` / `label_plural` name the EVENT for counting — "bordereau setup
    activated" / "bordereau setups activated" — so the UI can summarise a batch
    as "3 bordereau setups activated" without a kind→wording table of its own.
    The call site supplies them because only it knows the right English; this
    module just carries the strings. Omitted → derived mechanically from `kind`.

    Returns the new notification id, or None if it couldn't be recorded.

    NEVER RAISES. Callers invoke this from the tail of a business action
    (activation, upload, …) and that action must succeed even when notification
    delivery does not. SMTP runs on a background thread by default so a slow or
    unreachable mail server can't add seconds to the user's request; pass
    ``send_email_async=False`` to send inline (tests, scripts)."""
    facts = list(facts or [])
    notif_id: Optional[int] = None
    try:
        payload = dict(details or {})
        # `facts` is the rendering contract for the feed row; keep it under a
        # reserved key so caller-supplied details can never collide with it.
        payload["facts"] = [[str(fact_label), None if value is None else str(value)]
                            for fact_label, value in facts]
        if link_path:
            payload["link_path"] = link_path
        if action:
            payload["action"] = action
        payload["label"] = label or _derived_label(kind)
        payload["label_plural"] = label_plural or payload["label"]
        with SessionLocal() as s:
            row = PlatformNotification(
                kind=kind, tenant_id=tenant_id, actor=actor, title=title,
                body=body, target=target, details=payload)
            s.add(row)
            s.commit()
            notif_id = row.id
    except Exception as e:  # noqa: BLE001
        log.exception("notify: could not record '%s' notification: %s", kind, e)

    try:
        recipients = platform_admin_recipients()
        if not recipients:
            log.warning("notify: no platform-admin recipients for '%s' — no app_user "
                        "currently holds the %s role", kind, PLATFORM_ADMIN_ROLE)
            return notif_id
        args = (recipients, title, body, facts, _app_link(link_path), link_label,
                subject or title, action)
        if send_email_async:
            threading.Thread(target=_send_emails, args=args, daemon=True,
                             name=f"notify-{kind}").start()
        else:
            _send_emails(*args)
    except Exception as e:  # noqa: BLE001
        log.exception("notify: could not dispatch '%s' emails: %s", kind, e)
    return notif_id


# ---------------------------------------------------------------------------
# read path
# ---------------------------------------------------------------------------

def _derived_label(kind: str) -> str:
    """Fallback event wording when a call site didn't supply one:
    'bordereau_setup_activated' → 'bordereau setup activated'. Mechanical, so a
    new kind always reads as something rather than as a raw identifier."""
    return (kind or "").replace("_", " ").strip() or "notification"


def _to_dict(row: PlatformNotification, seen_at: Optional[datetime],
             tenant_names: dict[int, str]) -> dict:
    details = dict(row.details or {})
    facts = [(f[0], f[1]) for f in details.pop("facts", []) if isinstance(f, (list, tuple)) and f]
    details.pop("label", None)
    details.pop("label_plural", None)
    return {
        "id": row.id,
        "kind": row.kind,
        "title": row.title,
        "body": row.body,
        "target": row.target,
        "actor": row.actor,
        "tenant_id": row.tenant_id,
        "tenant_name": tenant_names.get(row.tenant_id),
        "facts": [{"label": lbl, "value": val} for lbl, val in facts],
        "link_path": details.pop("link_path", None),
        "action": details.pop("action", None),
        "details": details,
        "created_at": _iso(row.created_at),
        "unread": seen_at is None or (row.created_at is not None
                                      and row.created_at > seen_at),
    }


def _iso(dt: Optional[datetime]) -> Optional[str]:
    """UTC ISO string with an explicit Z — the stored columns are naive UTC, and
    without the marker the browser parses them as local time."""
    if dt is None:
        return None
    from datetime import timezone
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def feed_for_user(user_id: int, limit: int = 20, kind: Optional[str] = None,
                  unread_only: bool = False) -> dict:
    """The notification feed for one platform admin.

    Returns ``{items, unread, unread_by_kind, seen_at, latest_at}``. `unread`
    counts EVERY unread notification, not just the ones in `items`, so a capped
    page still reports the true number, and `unread_by_kind` breaks that total
    down with the wording each kind was written with — enough for the UI to say
    "3 bordereau setups activated" without knowing what a setup is. `latest_at`
    is the newest notification's timestamp — the client hands it back to
    mark_read so nothing that arrived after the page was rendered is silently
    marked as seen."""
    from db import Tenant
    with SessionLocal() as s:
        user = s.get(AppUser, user_id)
        seen_at = user.notifications_seen_at if user else None

        base = s.query(PlatformNotification)
        if kind:
            base = base.filter(PlatformNotification.kind == kind)

        unread_q = base
        if seen_at is not None:
            unread_q = unread_q.filter(PlatformNotification.created_at > seen_at)
        unread = unread_q.order_by(None).count()

        # Per-kind unread totals, each carrying the singular/plural wording its
        # own most recent notification was written with.
        unread_by_kind = []
        for k, count in (unread_q.order_by(None)
                         .with_entities(PlatformNotification.kind,
                                        func.count(PlatformNotification.id))
                         .group_by(PlatformNotification.kind).all()):
            newest = (unread_q.filter(PlatformNotification.kind == k)
                      .order_by(PlatformNotification.created_at.desc()).first())
            d = (newest.details or {}) if newest else {}
            singular = d.get("label") or _derived_label(k)
            unread_by_kind.append({
                "kind": k, "count": int(count), "label": singular,
                "label_plural": d.get("label_plural") or singular,
            })
        unread_by_kind.sort(key=lambda r: (-r["count"], r["kind"]))

        latest_at = base.with_entities(
            func.max(PlatformNotification.created_at)).scalar()

        rows = ((unread_q if unread_only else base)
                .order_by(PlatformNotification.created_at.desc(),
                          PlatformNotification.id.desc())
                .limit(max(1, min(limit, 200))).all())

        tenant_ids = {r.tenant_id for r in rows if r.tenant_id is not None}
        tenant_names: dict[int, str] = {}
        if tenant_ids:
            for tid, legal, code in (s.query(Tenant.id, Tenant.legal_name,
                                             Tenant.tenant_name)
                                     .filter(Tenant.id.in_(tenant_ids)).all()):
                tenant_names[tid] = legal or code

        return {
            "items": [_to_dict(r, seen_at, tenant_names) for r in rows],
            "unread": int(unread),
            "unread_by_kind": unread_by_kind,
            "seen_at": _iso(seen_at),
            "latest_at": _iso(latest_at),
        }


def mark_read(user_id: int, upto: Optional[datetime] = None) -> dict:
    """Move a platform admin's read watermark forward to `upto` (default now).

    Only ever moves FORWARD: a stale client sending an old timestamp can't
    resurrect notifications the admin has already cleared."""
    now = datetime.utcnow()
    watermark = upto or now
    # Never accept a future timestamp — that would mark unseen future arrivals
    # as read.
    if watermark > now:
        watermark = now
    with SessionLocal() as s:
        user = s.get(AppUser, user_id)
        if user is None:
            return {"unread": 0, "seen_at": None}
        if user.notifications_seen_at is None or user.notifications_seen_at < watermark:
            user.notifications_seen_at = watermark
            s.commit()
        seen_at = user.notifications_seen_at
        unread = (s.query(PlatformNotification)
                  .filter(PlatformNotification.created_at > seen_at)
                  .order_by(None).count())
        return {"unread": int(unread), "seen_at": _iso(seen_at)}


def is_platform_admin_role(raw_role: str | None) -> bool:
    """True when a stored/legacy role string is the platform-admin role."""
    return normalize_role(raw_role) == PLATFORM_ADMIN_ROLE
