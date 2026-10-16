"""The two emails a reinsurer's treaty link sends.

  share   "<carrier> has shared <treaty> with you" + the link + the code
  code    a fresh code and nothing else

Same card, same code block and the same sender as the signing emails
(esign_email), because the reader is in the same position: somebody with no
account here, deciding from the email alone whether it is genuine.
"""
from __future__ import annotations

import logging

from esign_email import _code_block, _esc, _facts, _shell

log = logging.getLogger("kavachio.treaty.email")


def share_email(*, link: str, name: str, carrier: str, reinsurer: str,
                treaty: str, type_label: str, term: str, expires: str,
                otp: str) -> str:
    return _shell(
        f"{carrier} has shared a treaty with you",
        f"""<p style="font-size:14px;color:#0E1320;line-height:1.6;margin:0 0 8px">
              Hi {_esc(name)},</p>
            <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 18px">
              {_esc(carrier)} uses Kavachio to keep its reinsurance treaties.
              They have given you a private, read-only view of
              <b>{_esc(treaty)}</b>, showing its structure and
              {_esc(reinsurer)}'s own share of it. No account is needed.</p>
            {_facts([("Treaty", treaty), ("Type", type_label), ("Term", term),
                     ("Shared by", carrier), ("Your organisation", reinsurer)])}
            <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 4px">
              You will see your own lines only. Other reinsurers on the treaty
              are not shown to you, and yours is not shown to them.</p>
            {_code_block(otp)}""",
        link=link, cta="View the treaty",
        footer=(f"This link is yours alone and works until {_esc(expires)}. "
                f"Opening it asks for the code above every time you start a "
                f"new browser session."))


def code_email(*, name: str, treaty: str, otp: str) -> str:
    """No link in it, on purpose — the one they already have still works, and
    a message holding both is one more thing that can be forwarded whole."""
    return _shell(
        f"Your code for {treaty}",
        f"""<p style="font-size:14px;color:#0E1320;line-height:1.6;margin:0 0 8px">
              Hi {_esc(name)},</p>
            <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 4px">
              Here is a new code for <b>{_esc(treaty)}</b>. It replaces any
              code you were sent before. Go back to the link in the earlier
              email and enter it there.</p>
            {_code_block(otp)}""")


def safe_send(to: str, subject: str, html: str, text: str) -> bool:
    """Send without failing the request — a link already issued must not be
    rolled back because SMTP was down. The carrier sees `emailed: false` and
    can resend. Sent from the NOTIFY mailbox, like the signing emails."""
    try:
        from email_utils import send_email
        from notifications import NOTIFY_MAIL_ACCOUNT
        send_email(to, subject, html, text=text, account=NOTIFY_MAIL_ACCOUNT)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("[treaty] email '%s' to %s failed: %s", subject, to, e)
        return False
