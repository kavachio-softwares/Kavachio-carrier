"""Outbound email over SMTP (password-reset links, invites, platform notices).

Sends via SMTP over SSL (port 465 by default) using the stdlib `smtplib` — no
extra dependency. EVERY setting comes from the environment; no address and no
credential is baked into this file.

Mail can go out as more than one sender, so accounts are NAMED:

    send_email(...)                     the DEFAULT account — SMTP_HOST,
                                        SMTP_PORT, SMTP_USER, SMTP_PASS,
                                        SMTP_FROM, SMTP_FROM_NAME.
                                        Used by password-reset and invite mail.

    send_email(..., account="NOTIFY")   a NAMED account — NOTIFY_SMTP_USER,
                                        NOTIFY_SMTP_PASS, … Anything that
                                        account doesn't set falls back to the
                                        unprefixed value, so a sender that only
                                        differs by mailbox needs just two vars.
                                        Used by platform-admin notifications.

A third sender needs no change here: pick a prefix and set its variables.

SMTP_PASS (and any <PREFIX>_SMTP_PASS) is an APP PASSWORD issued by the mailbox
provider, never the account's own sign-in password.

MAIL_ALLOWED_RECIPIENTS is a NON-PRODUCTION guard: set it to one or more
addresses and mail is delivered only to those, with every other recipient
skipped and logged. It lets a real flow be exercised end to end without
reaching real users. Leave it UNSET in production — it silently suppresses
mail to everyone it doesn't list.

Config is resolved at CALL time, not import time, so a process that loads its
.env after importing this module still sends with the right account.
"""
import os
import ssl
import smtplib
import logging
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr

log = logging.getLogger("bdx.email")


def _allowed_recipients() -> set[str]:
    """The test-mode delivery allowlist from MAIL_ALLOWED_RECIPIENTS.

    Empty set = the guard is OFF and mail goes to whoever it is addressed to
    (the normal, production behaviour). When the variable lists one or more
    addresses, `send_email` delivers ONLY to those and skips everything else —
    so you can exercise a real flow end to end without mailing real users.

    Addresses are matched exactly, case-insensitively. Comma, semicolon or
    newline separated."""
    raw = (os.getenv("MAIL_ALLOWED_RECIPIENTS") or "").strip()
    if not raw:
        return set()
    parts = raw.replace(";", ",").replace("\n", ",").split(",")
    return {p.strip().lower() for p in parts if p.strip()}


def _env(prefix: str, name: str, default: str = "") -> str:
    """`<PREFIX>_<NAME>` when set, else `<NAME>`, else `default`.

    An empty string counts as unset at both levels, so a blank override can
    never silently disable an otherwise working account."""
    if prefix:
        scoped = (os.getenv(f"{prefix}_{name}") or "").strip()
        if scoped:
            return scoped
    return (os.getenv(name) or "").strip() or default


@dataclass(frozen=True)
class MailAccount:
    """One resolved SMTP identity: where to connect, who to log in as, and what
    address the message is From."""
    host: str
    port: int
    user: str
    password: str
    sender: str
    sender_name: str


def mail_account(account: str = "") -> MailAccount:
    """Resolve the named SMTP account ("" = the default one)."""
    prefix = (account or "").strip().upper()
    user = _env(prefix, "SMTP_USER")
    return MailAccount(
        host=_env(prefix, "SMTP_HOST", "smtp.gmail.com"),
        port=int(_env(prefix, "SMTP_PORT", "465")),
        user=user,
        # Providers display app passwords as four space-separated groups; the
        # spaces are presentation only, so a value pasted in that form works
        # exactly like the joined 16 characters.
        password=_env(prefix, "SMTP_PASS").replace(" ", ""),
        # From defaults to the authenticated mailbox: most providers reject a
        # From they haven't authorised for that login.
        sender=_env(prefix, "SMTP_FROM") or user,
        sender_name=_env(prefix, "SMTP_FROM_NAME", "Kavachio"),
    )


def send_email(to: str, subject: str, html: str, text: str | None = None,
               account: str = "", cc: "str | list[str] | None" = None,
               attachments: "list[tuple[str, bytes, str]] | None" = None) -> None:
    """Send an HTML email as `account` (default sender when omitted).
    Raises on failure — the caller decides how to handle it.

    `cc` is one address or a list — e.g. the carrier user who asked for a
    reminder to go out, so they hold a copy of exactly what the broker was
    told. Left off the message entirely when empty, rather than an empty
    header.

    `attachments` is a list of (filename, data, mime_type) — e.g. the completed
    contract sent to both parties when the last signature lands. A signed
    contract has to ARRIVE, not sit behind a login: the people who need it next
    are lawyers, auditors and reinsurers with no account on this platform.

    Honours the MAIL_ALLOWED_RECIPIENTS test guard: when that is set, a
    recipient not on the list is skipped (logged, not raised) so callers that
    fan out to several addresses still deliver to the allowed ones. Cc
    addresses go through the same guard, individually — one disallowed cc
    does not stop the message reaching an allowed `to`."""
    allowed = _allowed_recipients()
    if allowed and (to or "").strip().lower() not in allowed:
        # WARNING, not INFO: mail that a caller believes it sent is silently not
        # arriving, and that must be obvious in the log rather than something
        # you discover from an empty inbox.
        log.warning("[Email] SKIPPED '%s' to %s — MAIL_ALLOWED_RECIPIENTS is set "
                    "and does not list this address (test mode)", subject, to)
        return
    if allowed:
        log.warning("[Email] test mode: MAIL_ALLOWED_RECIPIENTS restricts delivery "
                    "to %d address(es); %s is allowed", len(allowed), to)
    cc_in = [cc] if isinstance(cc, str) else list(cc or [])
    cc_list = [c.strip() for c in cc_in if (c or "").strip()]
    if allowed:
        cc_list = [c for c in cc_list if c.strip().lower() in allowed]
    acct = mail_account(account)
    if not acct.user or not acct.password:
        prefix = (account or "").strip().upper()
        scoped = f"{prefix}_SMTP_USER / {prefix}_SMTP_PASS or " if prefix else ""
        raise RuntimeError(
            f"outbound email is not configured — set {scoped}"
            "SMTP_USER / SMTP_PASS in the environment."
        )
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((acct.sender_name, acct.sender))
    msg["To"] = to
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg.set_content(text or "Open this message in an HTML-capable email client.")
    msg.add_alternative(html, subtype="html")
    for name, data, mime in (attachments or []):
        # A bad mime string would raise inside the email package and lose the
        # whole message, so anything unexpected falls back to opaque bytes —
        # every client can still save the file.
        maintype, _, subtype = (mime or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype or "application",
                           subtype=subtype or "octet-stream", filename=name)

    log.info("[Email] sending '%s' to %s%s as %s via %s:%s",
             subject, to, f" (cc {', '.join(cc_list)})" if cc_list else "",
             acct.sender, acct.host, acct.port)
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(acct.host, acct.port, context=ctx, timeout=20) as server:
        server.login(acct.user, acct.password)
        # No explicit to_addrs: smtplib pulls recipients from the To/Cc headers
        # already on the message, so leaving `cc` off never sends bcc-style.
        server.send_message(msg)
    log.info("[Email] sent to %s as %s", to, acct.sender)


def reset_email_html(link: str, name: str | None = None) -> str:
    """Branded HTML body for the password-reset email. Greets the recipient by
    name when provided."""
    greeting = f"Hi {name}," if (name or "").strip() else "Hi,"
    return f"""\
<!doctype html><html><body style="margin:0;background:#F3F4F7;font-family:Inter,Arial,sans-serif">
  <div style="max-width:600px;margin:0 auto;padding:44px 20px">
    <div style="background:#fff;border:1px solid #E5E8EE;border-radius:16px;padding:48px 46px;
                box-shadow:0 10px 26px -10px rgba(14,19,32,.12)">
      <div style="font-family:'Space Grotesk',Inter,Arial,sans-serif;font-size:20px;font-weight:700;
                  color:#0E1320;margin-bottom:6px">Kavachio</div>
      <div style="font-size:13px;color:#8B93A2;margin-bottom:22px">Bordereau Management</div>
      <h1 style="font-size:18px;color:#0E1320;margin:0 0 10px">Reset your password</h1>
      <p style="font-size:14px;color:#0E1320;line-height:1.6;margin:0 0 10px">{greeting}</p>
      <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 22px">
        We received a request to reset the password for your Kavachio account.
        Click the button below to choose a new password. This link expires in
        <b>30 minutes</b>.
      </p>
      <a href="{link}" style="display:inline-block;background:#3149C6;color:#fff;text-decoration:none;
                font-size:14px;font-weight:600;padding:12px 22px;border-radius:8px">Reset password</a>
      <p style="font-size:12px;color:#8B93A2;line-height:1.6;margin:22px 0 0">
        If the button doesn't work, copy and paste this link into your browser:<br>
        <a href="{link}" style="color:#3149C6;word-break:break-all">{link}</a>
      </p>
      <p style="font-size:12px;color:#8B93A2;line-height:1.6;margin:18px 0 0">
        Didn't request this? You can safely ignore this email — your password won't change.
      </p>
    </div>
  </div>
</body></html>"""


def invite_email_html(link: str, name: str | None = None, org: str | None = None) -> str:
    """Branded HTML body for a user-invite email — a tokened 'complete your
    onboarding' link (the same reset page, in invite mode). Greets by name and
    names the organization when provided."""
    greeting = f"Hi {name}," if (name or "").strip() else "Hi,"
    org_txt = f" to <b>{org}</b> on Kavachio" if (org or "").strip() else " to Kavachio"
    return f"""\
<!doctype html><html><body style="margin:0;background:#F3F4F7;font-family:Inter,Arial,sans-serif">
  <div style="max-width:600px;margin:0 auto;padding:44px 20px">
    <div style="background:#fff;border:1px solid #E5E8EE;border-radius:16px;padding:48px 46px;
                box-shadow:0 10px 26px -10px rgba(14,19,32,.12)">
      <div style="font-family:'Space Grotesk',Inter,Arial,sans-serif;font-size:20px;font-weight:700;
                  color:#0E1320;margin-bottom:6px">Kavachio</div>
      <div style="font-size:13px;color:#8B93A2;margin-bottom:22px">Bordereau Management</div>
      <h1 style="font-size:18px;color:#0E1320;margin:0 0 10px">You're invited</h1>
      <p style="font-size:14px;color:#0E1320;line-height:1.6;margin:0 0 10px">{greeting}</p>
      <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 22px">
        You've been invited{org_txt}. Click the button below to set up your
        password and complete your onboarding. This link expires in <b>7 days</b>.
      </p>
      <a href="{link}" style="display:inline-block;background:#3149C6;color:#fff;text-decoration:none;
                font-size:14px;font-weight:600;padding:12px 22px;border-radius:8px">Complete onboarding</a>
      <p style="font-size:12px;color:#8B93A2;line-height:1.6;margin:22px 0 0">
        If the button doesn't work, copy and paste this link into your browser:<br>
        <a href="{link}" style="color:#3149C6;word-break:break-all">{link}</a>
      </p>
      <p style="font-size:12px;color:#8B93A2;line-height:1.6;margin:18px 0 0">
        If you weren't expecting this invitation, you can safely ignore this email.
      </p>
    </div>
  </div>
</body></html>"""


def carrier_invite_email_html(link: str, name: str | None = None,
                              carrier: str | None = None) -> str:
    """A carrier inviting a broker who ALREADY has a Kavachio login.

    Different from invite_email_html on purpose. That one hands somebody their
    account — set a password, complete onboarding. This one goes to a person
    who already has an account and is being asked a question: another carrier
    would like to work with you. So there is no password, no expiry, and the
    button says JOIN rather than "complete onboarding" — it drops them on the
    invitation screen inside the app, signed in as themselves.
    """
    greeting = f"Hi {name}," if (name or "").strip() else "Hi,"
    who = f"<b>{carrier}</b>" if (carrier or "").strip() else "A carrier"
    return f"""\
<!doctype html><html><body style="margin:0;background:#F3F4F7;font-family:Inter,Arial,sans-serif">
  <div style="max-width:600px;margin:0 auto;padding:44px 20px">
    <div style="background:#fff;border:1px solid #E5E8EE;border-radius:16px;padding:48px 46px;
                box-shadow:0 10px 26px -10px rgba(14,19,32,.12)">
      <div style="font-family:'Space Grotesk',Inter,Arial,sans-serif;font-size:20px;font-weight:700;
                  color:#0E1320;margin-bottom:6px">Kavachio</div>
      <div style="font-size:13px;color:#8B93A2;margin-bottom:22px">Bordereau Management</div>
      <h1 style="font-size:18px;color:#0E1320;margin:0 0 10px">{carrier or "A carrier"} wants to work with you</h1>
      <p style="font-size:14px;color:#0E1320;line-height:1.6;margin:0 0 10px">{greeting}</p>
      <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 22px">
        {who} has invited you to work with them on Kavachio. You already have an
        account &mdash; nothing to set up. Join and they can put you on their
        programmes; your other carriers are not affected and are never shown to
        them.
      </p>
      <a href="{link}" style="display:inline-block;background:#3149C6;color:#fff;text-decoration:none;
                font-size:14px;font-weight:600;padding:12px 22px;border-radius:8px">Join now</a>
      <p style="font-size:12px;color:#8B93A2;line-height:1.6;margin:22px 0 0">
        If the button doesn't work, copy and paste this link into your browser:<br>
        <a href="{link}" style="color:#3149C6;word-break:break-all">{link}</a>
      </p>
      <p style="font-size:12px;color:#8B93A2;line-height:1.6;margin:18px 0 0">
        You can decline, and nothing happens. If you weren't expecting this, you
        can safely ignore this email.
      </p>
    </div>
  </div>
</body></html>"""


def overdue_reminder_email_html(name: str | None, program_name: str, period: str,
                                due_date: str | None, days_over: int,
                                note: str | None = None) -> str:
    """A carrier chasing a broker for a bordereau that is now late.

    Plain and short on purpose — this is a nudge about one file, not a
    marketing email. `note` is whatever the carrier user typed in "Anything to
    add?"; shown only when they wrote one.
    """
    greeting = f"Hi {name}," if (name or "").strip() else "Hi,"
    due_txt = f" It was due on <b>{due_date}</b>." if due_date else ""
    return f"""\
<!doctype html><html><body style="margin:0;background:#F3F4F7;font-family:Inter,Arial,sans-serif">
  <div style="max-width:600px;margin:0 auto;padding:44px 20px">
    <div style="background:#fff;border:1px solid #E5E8EE;border-radius:16px;padding:48px 46px;
                box-shadow:0 10px 26px -10px rgba(14,19,32,.12)">
      <div style="font-family:'Space Grotesk',Inter,Arial,sans-serif;font-size:20px;font-weight:700;
                  color:#0E1320;margin-bottom:6px">Kavachio</div>
      <div style="font-size:13px;color:#8B93A2;margin-bottom:22px">Bordereau Management</div>
      <h1 style="font-size:18px;color:#0E1320;margin:0 0 10px">
        {program_name} — {period} bordereau is {plural(days_over, 'day')} overdue
      </h1>
      <p style="font-size:14px;color:#0E1320;line-height:1.6;margin:0 0 10px">{greeting}</p>
      <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 10px">
        The <b>{period}</b> bordereau for <b>{program_name}</b> hasn't arrived yet.{due_txt}
        Could you send it across when you get a chance?
      </p>
      {f'<p style="font-size:14px;color:#0E1320;line-height:1.6;margin:0 0 22px;'
       f'padding:12px 14px;background:#F8F9FC;border-radius:8px">{escape_html(note)}</p>' if (note or "").strip() else ""}
      <p style="font-size:12px;color:#8B93A2;line-height:1.6;margin:22px 0 0">
        This is a reminder from your carrier on Kavachio.
      </p>
    </div>
  </div>
</body></html>"""


def bordereau_sent_email_html(program_name: str, period: str, due_date: str | None,
                              sent_date: str) -> str:
    """A broker sending a processed bordereau on to the carrier. The file
    itself is the attachment; this is just the covering note.

    Deliberately says nothing about on-time/late — that judgement belongs on
    the carrier's own calendar and to the recipient reading the due date
    themselves, not to a tone in the covering email.
    """
    due_txt = f" It was due on <b>{due_date}</b>." if due_date else ""
    return f"""\
<!doctype html><html><body style="margin:0;background:#F3F4F7;font-family:Inter,Arial,sans-serif">
  <div style="max-width:600px;margin:0 auto;padding:44px 20px">
    <div style="background:#fff;border:1px solid #E5E8EE;border-radius:16px;padding:48px 46px;
                box-shadow:0 10px 26px -10px rgba(14,19,32,.12)">
      <div style="font-family:'Space Grotesk',Inter,Arial,sans-serif;font-size:20px;font-weight:700;
                  color:#0E1320;margin-bottom:6px">Kavachio</div>
      <div style="font-size:13px;color:#8B93A2;margin-bottom:22px">Bordereau Management</div>
      <h1 style="font-size:18px;color:#0E1320;margin:0 0 10px">
        {program_name} — {period} bordereau
      </h1>
      <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 10px">
        The <b>{period}</b> bordereau for <b>{program_name}</b> is attached, sent on
        <b>{sent_date}</b>.{due_txt}
      </p>
      <p style="font-size:12px;color:#8B93A2;line-height:1.6;margin:22px 0 0">
        Sent from your broker on Kavachio.
      </p>
    </div>
  </div>
</body></html>"""


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def escape_html(text: str) -> str:
    from html import escape
    return escape(text)
