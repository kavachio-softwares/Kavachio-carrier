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


def approve_email(*, link: str, name: str, carrier: str, reinsurer: str,
                  treaty: str, type_label: str, term: str, expires: str,
                  otp: str) -> str:
    """A draft treaty sent to a reinsurer to approve, or to ask for changes —
    the same private link, which carries the two buttons."""
    return _shell(
        f"{carrier} asks you to approve a treaty",
        f"""<p style="font-size:14px;color:#0E1320;line-height:1.6;margin:0 0 8px">
              Hi {_esc(name)},</p>
            <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 18px">
              {_esc(carrier)} asks you to review <b>{_esc(treaty)}</b> for
              {_esc(reinsurer)}. Open the link, enter the code below, and
              approve it, or ask for changes. No account is needed.</p>
            {_facts([("Treaty", treaty), ("Type", type_label), ("Term", term),
                     ("Sent by", carrier), ("Your organisation", reinsurer)])}
            <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 4px">
              You will see your own lines only. Once every reinsurer has
              approved, {_esc(carrier)} signs it and asks you to sign.</p>
            {_code_block(otp)}""",
        link=link, cta="Review the treaty",
        footer=(f"This link is yours alone and works until {_esc(expires)}. "
                f"Opening it asks for the code above every time you start a "
                f"new browser session."))


def endorsement_email(*, link: str, name: str, carrier: str, reinsurer: str,
                      treaty: str, endorsement: str, effective: str,
                      changes: list[str], expires: str, otp: str,
                      sign: bool = False) -> str:
    """An endorsement to a treaty, sent to a reinsurer to approve — or, once
    every reinsurer approved it and the carrier signed it, to sign. It is used
    only once every reinsurer has signed it."""
    items = "".join(f"<li>{_esc(x)}</li>" for x in changes)
    if sign:
        return _shell(
            f"{carrier} asks you to sign an endorsement",
            f"""<p style="font-size:14px;color:#0E1320;line-height:1.6;margin:0 0 8px">
                  Hi {_esc(name)},</p>
                <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 18px">
                  Every reinsurer approved <b>{_esc(endorsement)}</b> to
                  <b>{_esc(treaty)}</b>, and {_esc(carrier)} has signed it. Open the
                  link, enter the code below, and sign it for {_esc(reinsurer)}.</p>
                {_facts([("Treaty", treaty), ("Endorsement", endorsement),
                         ("Takes effect", effective or "—"), ("Signed by", carrier)])}
                {f'<ul style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 12px;padding-left:18px">{items}</ul>' if items else ''}
                <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 4px">
                  It is in force once every reinsurer on the treaty has signed it.</p>
                {_code_block(otp)}""",
            link=link, cta="Review and sign",
            footer=(f"This link is yours alone and works until {_esc(expires)}. "
                    f"Opening it asks for the code above every time you start a "
                    f"new browser session."))
    return _shell(
        f"{carrier} asks you to approve an endorsement",
        f"""<p style="font-size:14px;color:#0E1320;line-height:1.6;margin:0 0 8px">
              Hi {_esc(name)},</p>
            <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 18px">
              {_esc(carrier)} has endorsed <b>{_esc(treaty)}</b> and asks
              {_esc(reinsurer)} to approve it. Open the link, enter the code
              below, and approve it, or ask for changes.</p>
            {_facts([("Treaty", treaty), ("Endorsement", endorsement),
                     ("Takes effect", effective or "—"), ("Sent by", carrier)])}
            {f'<ul style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 12px;padding-left:18px">{items}</ul>' if items else ''}
            <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 4px">
              Once every reinsurer has approved it, {_esc(carrier)} signs it and
              asks you to sign. Nothing changes until every reinsurer has
              signed it.</p>
            {_code_block(otp)}""",
        link=link, cta="Review the endorsement",
        footer=(f"This link is yours alone and works until {_esc(expires)}. "
                f"Opening it asks for the code above every time you start a "
                f"new browser session."))


def basis_email(*, link: str, name: str, carrier: str, reinsurer: str,
                treaty: str, changes: list[str], expires: str, otp: str) -> str:
    """Changes to a treaty's terms (its calculation basis), sent to a
    reinsurer to approve. They are used only once every reinsurer approves."""
    items = "".join(f"<li>{_esc(x)}</li>" for x in changes)
    return _shell(
        f"{carrier} asks you to approve changes to a treaty",
        f"""<p style="font-size:14px;color:#0E1320;line-height:1.6;margin:0 0 8px">
              Hi {_esc(name)},</p>
            <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 18px">
              {_esc(carrier)} wants to change the terms of <b>{_esc(treaty)}</b>
              and asks {_esc(reinsurer)} to approve. Open the link, enter the
              code below, and approve the changes, or ask for changes.</p>
            {_facts([("Treaty", treaty), ("Sent by", carrier)])}
            {f'<ul style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 12px;padding-left:18px">{items}</ul>' if items else ''}
            <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 4px">
              Nothing changes until every reinsurer on the treaty has approved.</p>
            {_code_block(otp)}""",
        link=link, cta="Review the changes",
        footer=(f"This link is yours alone and works until {_esc(expires)}. "
                f"Opening it asks for the code above every time you start a "
                f"new browser session."))


def answer_email(*, name: str, reinsurer: str, what: str, approved: bool,
                 note: str | None, link: str, all_approved: bool) -> str:
    """To the carrier: a reinsurer answered a treaty or an endorsement."""
    verb = "approved" if approved else "asked for changes to"
    after = ""
    if approved and all_approved:
        after = "Every reinsurer has now approved it."
    return _shell(
        f"{reinsurer} {verb} {what}",
        f"""<p style="font-size:14px;color:#0E1320;line-height:1.6;margin:0 0 8px">
              Hi {_esc(name)},</p>
            <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 12px">
              <b>{_esc(reinsurer)}</b> {verb} <b>{_esc(what)}</b>. {_esc(after)}</p>
            {f'<p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 4px"><b>Their note:</b> {_esc(note)}</p>' if note else ''}""",
        link=link, cta="Open it in Kavachio")


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
