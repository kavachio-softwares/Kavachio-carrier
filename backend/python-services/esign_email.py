"""The four emails a signing round sends.

Signing happens entirely in email. Nobody in this flow is asked to have an
account: the broker's signer gets a link, opens it, signs, and never sees a
login screen. So these messages are not notifications ABOUT the work — they are
where the work happens, and they say plainly what the reader is being asked to
do and what they are agreeing to.

  ask       "This is ready for your signature."           → the current signer
  handover  "The insurer has signed. It is now with you."  → the next signer
  done      "Everybody has signed."  + the PDF attached    → everybody
  declined  "They asked for a change, and here is why."    → whoever sent it

The house style is the one already set by reset_email_html / invite_email_html
in email_utils: one card, one button, the link repeated as text underneath for
the clients that strip buttons.
"""
from __future__ import annotations

import html as _html
import logging

log = logging.getLogger("bdx.esign.email")

_BRAND = "#077282"


def _esc(v: object) -> str:
    """Everything interpolated below is a name, an organisation or a reason
    typed by a person. None of it may become markup."""
    return _html.escape(str(v or ""), quote=True)


def _shell(heading: str, body_html: str, *, link: str | None = None,
           cta: str | None = None, footer: str | None = None) -> str:
    button = ""
    if link and cta:
        button = f"""
      <a href="{_esc(link)}" style="display:inline-block;background:{_BRAND};color:#fff;
                text-decoration:none;font-size:14px;font-weight:600;padding:12px 22px;
                border-radius:8px">{_esc(cta)}</a>
      <p style="font-size:12px;color:#8B93A2;line-height:1.6;margin:22px 0 0">
        If the button doesn't work, copy and paste this link into your browser:<br>
        <a href="{_esc(link)}" style="color:{_BRAND};word-break:break-all">{_esc(link)}</a>
      </p>"""
    tail = ""
    if footer:
        tail = (f'<p style="font-size:12px;color:#8B93A2;line-height:1.6;margin:18px 0 0">'
                f'{footer}</p>')
    return f"""\
<!doctype html><html><body style="margin:0;background:#F3F4F7;font-family:Inter,Arial,sans-serif">
  <div style="max-width:620px;margin:0 auto;padding:44px 20px">
    <div style="background:#fff;border:1px solid #E5E8EE;border-radius:16px;padding:44px 42px;
                box-shadow:0 10px 26px -10px rgba(14,19,32,.12)">
      <div style="font-family:'Space Grotesk',Inter,Arial,sans-serif;font-size:20px;font-weight:700;
                  color:#0E1320;margin-bottom:6px">Kavachio</div>
      <div style="font-size:13px;color:#8B93A2;margin-bottom:22px">Bordereau platform</div>
      <h1 style="font-size:18px;color:#0E1320;margin:0 0 14px">{_esc(heading)}</h1>
      {body_html}
      {button}
      {tail}
    </div>
  </div>
</body></html>"""


def _facts(rows: list[tuple[str, str]]) -> str:
    """The small key/value table every one of these emails carries. A signer
    should be able to tell what they are signing without opening it."""
    cells = "".join(
        f"""<tr>
              <td style="padding:5px 14px 5px 0;font-size:12.5px;color:#8B93A2;
                         white-space:nowrap;vertical-align:top">{_esc(k)}</td>
              <td style="padding:5px 0;font-size:12.5px;color:#0E1320;font-weight:600">{_esc(v)}</td>
            </tr>""" for k, v in rows if v)
    return f"""<table style="border-collapse:collapse;margin:0 0 22px;
                   border-top:1px solid #EDEFF3;border-bottom:1px solid #EDEFF3;
                   padding:6px 0;width:100%"><tbody>{cells}</tbody></table>"""


def _code_block(otp: str) -> str:
    """The code, shown the way a code has to be shown: big, spaced, and
    impossible to mistake for a reference number.

    It sits BELOW the button, deliberately. Somebody who clicks first and reads
    second — which is most people — meets the code prompt on the page and then
    finds the code exactly where they left off in the email."""
    if not otp:
        return ""
    spaced = " ".join(otp)
    return f"""
      <div style="margin:24px 0 4px;padding:18px 20px;border:1px solid #CFE3E7;
                  border-radius:12px;background:#F4FBFC;text-align:center">
        <div style="font-size:12px;color:#5A7F86;font-weight:600;
                    letter-spacing:.04em;text-transform:uppercase">
          Your one-time code</div>
        <div style="font-family:'SF Mono',Menlo,Consolas,monospace;font-size:30px;
                    font-weight:700;color:#0B4A54;letter-spacing:.22em;margin:8px 0 6px">
          {_esc(spaced)}</div>
        <div style="font-size:12px;color:#5A7F86;line-height:1.55">
          You will be asked for this after opening the link. It works only for
          you, and only for this contract.</div>
      </div>
      <p style="font-size:12px;color:#8B93A2;line-height:1.6;margin:12px 0 22px">
        Nobody at Kavachio will ever ask you for this code. If you did not expect
        this contract, do not enter it — tell the sender instead.
      </p>"""


def sign_request_email(*, link: str, signer_name: str, title: str, org_from: str,
                       counterparty: str, programme: str, term: str,
                       expires_days: int, first: bool, otp: str = "") -> str:
    """Asks the CURRENT signer to sign. `first` distinguishes the insurer who
    starts the round from anyone signing later without a handover."""
    lead = ("You are the first to sign. Once you have, it goes on to "
            f"{_esc(counterparty)} automatically." if first else
            f"It is your turn to sign. {_esc(counterparty)} is waiting on you.")
    return _shell(
        f"{title} is ready for your signature",
        f"""<p style="font-size:14px;color:#0E1320;line-height:1.6;margin:0 0 8px">
              Hi {_esc(signer_name)},</p>
            <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 18px">
              {_esc(org_from)} has sent you a contract to sign. {lead}</p>
            {_facts([("Contract", title), ("Programme", programme), ("Term", term),
                     ("Sent by", org_from), ("Other party", counterparty)])}
            <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 4px">
              You will be shown the whole document and only your own signature
              boxes are fillable — the other side's blocks are visible but locked
              to you. No account is needed.</p>
            {_code_block(otp)}""",
        link=link, cta="Read it and sign",
        footer=f"This link is yours alone and expires in {expires_days} days.")


def handover_email(*, link: str, signer_name: str, title: str, signed_by_org: str,
                   signed_by_name: str, signed_at: str, programme: str, term: str,
                   expires_days: int, otp: str = "") -> str:
    """The one the whole flow exists for: the insurer has signed, and the same
    document — carrying that signature — is now the broker's to sign."""
    return _shell(
        f"{signed_by_org} has signed {title}",
        f"""<p style="font-size:14px;color:#0E1320;line-height:1.6;margin:0 0 8px">
              Hi {_esc(signer_name)},</p>
            <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 18px">
              {_esc(signed_by_name)} signed for {_esc(signed_by_org)} on
              {_esc(signed_at)}. The contract is now with you, and the copy you
              open already carries their signature — it is the same document,
              not a new one.</p>
            {_facts([("Contract", title), ("Programme", programme), ("Term", term),
                     ("Already signed", f"{signed_by_org} — {signed_by_name}"),
                     ("Waiting on", "You")])}
            <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 4px">
              Only your own boxes are fillable. If something in the wording is
              wrong, decline with a reason rather than signing — it goes straight
              back to {_esc(signed_by_org)} and nobody ends up holding two
              versions.</p>
            {_code_block(otp)}""",
        link=link, cta="Read it and sign",
        footer=f"This link is yours alone and expires in {expires_days} days.")


def completed_email(*, title: str, programme: str, term: str,
                    signers: list[str], app_link: str | None = None) -> str:
    """Everybody signed. The PDF rides along as an attachment."""
    who = "".join(
        f'<li style="margin:0 0 5px">{_esc(s)}</li>' for s in signers)
    return _shell(
        f"{title} is fully signed",
        f"""<p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 18px">
              Everybody has signed. The completed contract is attached. It
              carries a digital seal, so your PDF reader will tell you if a
              single character of it is ever altered.</p>
            {_facts([("Contract", title), ("Programme", programme), ("Term", term)])}
            <p style="font-size:13px;color:#8B93A2;margin:0 0 6px">Signed by</p>
            <ul style="font-size:13.5px;color:#0E1320;line-height:1.6;margin:0 0 20px;
                       padding-left:18px">{who}</ul>
            <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 20px">
              The limits in the Schedule are now live checks: they run on every
              bordereau sent under this contract from today.</p>""",
        link=app_link, cta="Open it in Kavachio" if app_link else None,
        footer="Keep the attached copy. It is the signed original and needs no "
               "account to open.")


def code_email(*, signer_name: str, title: str, otp: str) -> str:
    """A fresh code and nothing else — for the signer who deleted the first
    email or left it too long. Deliberately carries NO link: the URL they
    already have still works, and a message with both in it is one more thing
    that can be forwarded whole."""
    return _shell(
        f"Your code for {title}",
        f"""<p style="font-size:14px;color:#0E1320;line-height:1.6;margin:0 0 8px">
              Hi {_esc(signer_name)},</p>
            <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 4px">
              Here is a new code for <b>{_esc(title)}</b>. It replaces any code
              you were sent before. Go back to the link in the earlier email and
              enter it there.</p>
            {_code_block(otp)}""")


def declined_email(*, title: str, declined_by_name: str, declined_by_org: str,
                   reason: str, app_link: str | None = None) -> str:
    """Somebody refused. Not a failure — a negotiation with the reason attached,
    which is the only version of this that is any use to the sender."""
    return _shell(
        f"{declined_by_org} asked for a change to {title}",
        f"""<p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 18px">
              {_esc(declined_by_name)} read {_esc(title)} and declined to sign it
              as written. Nothing was signed and no half-agreed version exists.</p>
            <div style="background:#FFF8E6;border:1px solid #F2E2B5;border-radius:10px;
                        padding:14px 16px;margin:0 0 20px">
              <div style="font-size:12px;color:#8B7024;font-weight:600;margin-bottom:5px">
                What they said</div>
              <div style="font-size:13.5px;color:#4A3C10;line-height:1.6">{_esc(reason)}</div>
            </div>
            <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 22px">
              Change the term they named and send it again. The previous round
              stays on the record with their reason on it.</p>""",
        link=app_link, cta="Open it in Kavachio" if app_link else None)
