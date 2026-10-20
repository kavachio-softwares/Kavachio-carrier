"""The two emails a reinsurer's treaty link sends.

  share   "<carrier> has shared <treaty> with you" + the link + the code
  sign    "<carrier> asks you to sign <treaty>" + the link + the code
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


def sign_email(*, link: str, name: str, carrier: str, reinsurer: str,
               treaty: str, type_label: str, term: str, expires: str,
               otp: str) -> str:
    """The lead reinsurer asked to sign — the same private link, which now
    carries a signature box."""
    return _shell(
        f"{carrier} asks you to sign a treaty",
        f"""<p style="font-size:14px;color:#0E1320;line-height:1.6;margin:0 0 8px">
              Hi {_esc(name)},</p>
            <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 18px">
              {_esc(carrier)} has signed <b>{_esc(treaty)}</b> and asks you to
              sign it for {_esc(reinsurer)}. Open the link, enter the code
              below, read the treaty and its wording, and type your name to
              sign. No account is needed.</p>
            {_facts([("Treaty", treaty), ("Type", type_label), ("Term", term),
                     ("Signed by", carrier), ("Signing for", reinsurer)])}
            <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 4px">
              Your signature is recorded with your name, this email address and
              the time — and it puts the treaty in force.</p>
            {_code_block(otp)}""",
        link=link, cta="Review and sign",
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


def statement_email(*, name: str, carrier: str, reinsurer: str, treaty: str,
                    period: str, version: int, currency: str | None,
                    totals: dict, previous: dict | None = None,
                    note: str | None = None, filename: str | None = None) -> str:
    """A reinsurer's bordereau for one period, with its own totals in the body
    and the BDX file attached — named in the body, so it is plain which file
    to open."""
    def m(v):
        if v is None:
            return None
        return f"{currency + ' ' if currency else ''}{v:,.2f}"
    facts = [("Treaty", treaty), ("Period", period),
             ("Version", f"{version} — replaces the one sent before" if version > 1
              else None),
             ("Ceded premium", m(totals.get("ceded_premium"))),
             ("Ceding commission", m(totals.get("ceding_commission"))),
             ("Net premium due", m(totals.get("net_premium"))),
             ("Claims paid (ceded)", m(totals.get("claims_paid"))),
             ("Recovery due", m(totals.get("recovery"))),
             ("Lines", str(totals.get("lines") or 0)),
             ("Attached", filename)]
    return _shell(
        f"Reinsurance bordereau — {period}",
        f"""<p style="font-size:14px;color:#0E1320;line-height:1.6;margin:0 0 8px">
              Hi {_esc(name)},</p>
            <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 18px">
              {_esc(carrier)} has sent {_esc(reinsurer)}'s reinsurance bordereau
              for <b>{_esc(treaty)}</b>, {_esc(period)}. Your statement is
              attached — it carries your share of every line, and your lines
              only.</p>
            {_facts([(k, v) for k, v in facts if v])}
            {f'<p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 4px">{_esc(note)}</p>' if note else ''}
            <p style="font-size:13px;color:#8B93A2;line-height:1.6;margin:12px 0 0">
              To agree it or raise a query, open the treaty from the private
              link {_esc(carrier)} sent you.</p>""")


def safe_send(to: str, subject: str, html: str, text: str,
              attachments: list | None = None, cc: list | None = None) -> bool:
    """Send without failing the request — a link already issued must not be
    rolled back because SMTP was down. The carrier sees `emailed: false` and
    can resend. Sent from the NOTIFY mailbox, like the signing emails."""
    try:
        from email_utils import send_email
        from notifications import NOTIFY_MAIL_ACCOUNT
        send_email(to, subject, html, text=text, account=NOTIFY_MAIL_ACCOUNT,
                   attachments=attachments, cc=cc or None)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("[treaty] email '%s' to %s failed: %s", subject, to, e)
        return False
