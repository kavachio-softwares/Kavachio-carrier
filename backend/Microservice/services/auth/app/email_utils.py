"""Outbound email over SMTP (used for password-reset links).

Sends via Gmail SMTP (SSL, port 465) using the stdlib `smtplib` — no extra
dependency. Configuration is read from environment variables, falling back to
the POC defaults below.

⚠️ The credentials are hardcoded as defaults for the POC. For production, set
   SMTP_USER / SMTP_PASS via environment / a secret manager and remove the
   literals here.
"""
import os
import ssl
import smtplib
import logging
from email.message import EmailMessage
from email.utils import formataddr

log = logging.getLogger("bdx.email")

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "support@kollabrt.com")
SMTP_PASS = os.getenv("SMTP_PASS", "seauprunzkaoedli")          # Gmail app password
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "Kavachio")


def send_email(to: str, subject: str, html: str, text: str | None = None) -> None:
    """Send an HTML email. Raises on failure (caller decides how to handle)."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((SMTP_FROM_NAME, SMTP_FROM))
    msg["To"] = to
    msg.set_content(text or "Open this message in an HTML-capable email client.")
    msg.add_alternative(html, subtype="html")

    log.info("[Email] sending '%s' to %s via %s:%s", subject, to, SMTP_HOST, SMTP_PORT)
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=20) as server:
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)
    log.info("[Email] sent to %s", to)


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
      <div style="font-size:13px;color:#8B93A2;margin-bottom:22px">Bordereau validation &amp; reporting</div>
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
