"""The broker's copy of "How They Send It".

A carrier sets up an email address or an SFTP folder for a broker, but it is
the broker who has to write the subject or name the file so that the programme,
contract and month can be read off it. The Configure Ingestion Channel dialog
shows the carrier an example; this sends the broker the same example, with the
rules for their own programmes, so the first file they send is not the one that
teaches them the format by being turned away.

Sent on the NOTIFY account behind the same switches as every other broker
email: BROKER_NOTIFY_ENABLED, and the MAIL_ALLOWED_RECIPIENTS test guard.
"""
from __future__ import annotations

import logging
import threading
from datetime import date, timedelta
from html import escape
from typing import Optional

log = logging.getLogger(__name__)

_CHANNEL_WORD = {"email": "Email", "sftp": "SFTP", "api": "API"}


def _example_month() -> date:
    """Last month: the one a bordereau sent today is usually for."""
    return date.today().replace(day=1) - timedelta(days=1)


def recipients(s, route) -> list[str]:
    """The channel's notify list, the address an email route sends FROM, and
    the broker's admins — the people who will be sending the files."""
    from submission_calendar_service import broker_contacts
    from submission_service import _EMAIL_RE, route_contacts
    out = list(route_contacts(route))
    if route.channel == "email" and _EMAIL_RE.match(route.address or ""):
        out.append(route.address)
    bid = route.broker_party_id
    out += [c["email"] for c in broker_contacts(s, [bid]).get(bid, []) if c.get("email")]
    seen, uniq = set(), []
    for e in out:
        k = e.strip().lower()
        if k and k not in seen:
            seen.add(k)
            uniq.append(e.strip())
    return uniq


def build(s, route, *, carrier: str, send_to: Optional[str],
          api_key: Optional[str] = None, api_base: Optional[str] = None) -> dict:
    """{subject, html, text, reply_to} for one route. An API route's email
    carries its new key (`api_key`, plaintext — only ever at the moment it is
    minted) and the endpoint (`api_base`)."""
    import intake_service as svc
    from db import Program
    from notifications import _email_text, notification_email_html

    tid, bid = route.tenant_id, route.broker_party_id
    progs = ([p for p in [s.get(Program, route.program_id)] if p is not None]
             if route.program_id else svc.broker_programmes(s, tid, bid))
    # (name, code, [(contract name, contract code), ...]) per programme.
    plist = [(p.name, svc.programme_code(p),
              [(svc.contract_label(c), svc.contract_code(c))
               for c in svc.live_contracts(s, tid, p.id, bid)])
             for p in progs]
    shared = not route.program_id

    d = _example_month()
    month, ym = d.strftime("%B %Y"), d.strftime("%Y-%m")
    prog, pcode, cons = plist[0] if plist else ("Programme", "PRG-XXXXXX", [])
    ccode = cons[0][1] if cons else None
    # The example uses the codes: short, and nothing to misspell.
    # Email goes to Kavachio's mailbox with the carrier in Cc — the Cc says
    # which carrier it is for, and a file without it is turned away.
    cc = None
    if route.channel == "email":
        import email_intake_service as mailsvc
        cc = mailsvc.carrier_cc(s, tid)
        where = "the email subject or the attachment's file name"
        example = (f"From:     {route.address}\n"
                   f"To:       {send_to or ''}\n"
                   f"Cc:       {cc or '<your carrier>'}\n"
                   f"Subject:  {pcode} - {f'{ccode} - ' if ccode else ''}{month}\n"
                   f"Attach:   the bordereau (.xlsx or .csv)")
    elif route.channel == "api":
        where = "each request"
        base = (api_base or "").rstrip("/")
        example = (f"curl -X POST {base}/v1/bordereaux \\\n"
                   f"  -H \"X-API-Key: {api_key or '<your API key>'}\" \\\n"
                   f"  -F \"file=@bordereau.xlsx\" \\\n"
                   f"  -F \"period={ym}\" \\\n"
                   f"  -F \"program_ref={pcode}\""
                   + (f" \\\n  -F \"contract_ref={ccode}\"" if ccode else ""))
    else:
        where = "the file name"
        example = (f"{svc.display_address(route)}/incoming/"
                   f"{pcode}_{f'{ccode}_' if ccode else ''}{ym}.xlsx")

    # What has to be in the subject / file name, and each code to write.
    named = lambda pairs: "; ".join(f"{c} = {n}" for n, c in pairs)   # noqa: E731
    api = route.channel == "api"
    rules = [("period" if api else "Month", "Required",
              f"e.g. “{ym}”" if api else f"e.g. “{month}” or “{ym}”")]
    if route.channel == "email":
        rules.insert(0, ("Cc", "Required", f"{cc or 'your carrier'} — without it the file is turned away"))
    rules.append(("program_ref" if api else "Programme",
                  "Required" if shared and len(plist) > 1 else "Optional",
                  named([(n, c) for n, c, _ in plist]) or "your programme"))
    for name, _, cs in plist:
        word = "contract_ref" if api else "Contract"
        label = f"{word} — {name}" if len(plist) > 1 else word
        rules.append((label, "Required" if len(cs) > 1 else "Optional",
                      named(cs) if cs else "no live contract yet"))

    rules_title = {
        "email": "What each email must have (programme and contract: code or name)",
        "api": "What each request must send (programme and contract: code or name)",
    }.get(route.channel, "What the file name must say (programme and contract: code or name)")
    how = {"email": "by email", "api": "through our API"}.get(route.channel, "by SFTP")
    title = (f"Your API access for sending bordereaux to {carrier}" if api
             else f"How to send your bordereaux to {carrier}")
    intro = (f"{carrier} has set up a way for you to send your bordereaux {how}"
             + (f" for {prog}." if not shared and plist else ".")
             + (" Your API key is below." if api and api_key else ""))
    more = [(f"Send each file as in the example. You can give the programme and "
             f"contract by code or by name — the code is shorter and avoids typing mistakes. "
             f"A request missing what is required is refused, with the reason and the valid "
             f"choices.") if api else
            (f"Send each file as in the example. We read the month, programme and "
             f"contract from {where}. You can use the code or the full name — the code is "
             f"shorter and avoids typing mistakes. A file without what is required is turned "
             f"away with the reason.")]
    if api:
        base = (api_base or "").rstrip("/")
        more += [f"GET {base}/v1/whoami with your key lists your programmes, contracts and "
                 f"reporting periods. Each reply carries a reference for the file — quote it "
                 f"if you contact {carrier}.",
                 f"Keep the key secret: anyone who has it can send files as you. If it is "
                 f"lost or exposed, ask {carrier} to revoke it and issue a new one."]
    elif route.channel == "sftp":
        more.append("Your SFTP login (user name and password, or key) is shared with you "
                    "separately.")
    more.append("After each file you will get an email with the result. If it has "
                "exceptions, the email includes a secure link to resolve them.")
    body = "\n\n".join([intro, *more])
    if api:
        where_to = ("Endpoint", f"POST {(api_base or '').rstrip('/')}/v1/bordereaux")
    elif route.channel == "email":
        where_to = ("Send to", send_to)
    else:
        where_to = ("Upload to", f"{svc.display_address(route)}/incoming")
    facts = [("Channel", _CHANNEL_WORD.get(route.channel, route.channel)),
             where_to,
             ("API key", api_key if api else None),
             ("Server", f"{svc.sftp_host()} (port {svc.sftp_port()})"
              if route.channel == "sftp" else None),
             ("Send from", route.address if route.channel == "email" else None),
             ("Cc", cc),
             ("Programme" if len(plist) == 1 else "Programmes",
              ", ".join(n for n, _, _ in plist) or None)]

    tone = {"Required": "#B42318", "Optional": "#667085"}
    cell = "padding:7px 10px;border-top:1px solid #EDEFF4;font-size:13px;color:#0E1320;vertical-align:top"
    rows = "".join(
        f"<tr><td style='{cell};font-weight:600;white-space:nowrap'>{escape(w)}</td>"
        f"<td style='{cell};color:{tone[r]};font-weight:600'>{r}</td>"
        f"<td style='{cell}'>{escape(n)}</td></tr>" for w, r, n in rules)
    para = "font-size:14.5px;color:#566071;line-height:1.65;margin:0 0 18px"
    extra = (
        "<div style='margin-top:24px'>"
        f"<p style='font-size:13px;font-weight:600;color:#111827;margin:0 0 6px'>"
        f"{escape(rules_title)}</p>"
        "<table role='presentation' style='border-collapse:collapse;width:100%;"
        f"border:1px solid #E5E8EE;border-radius:8px;margin:0 0 18px'>{rows}</table>"
        "<p style='font-size:13px;font-weight:600;color:#111827;margin:0 0 6px'>Example</p>"
        "<pre style='margin:0;padding:12px 14px;background:#F4F6FA;border:1px solid #E5E8EE;"
        "border-radius:8px;font:12px/1.6 Menlo,Consolas,monospace;color:#0E1320;"
        f"white-space:pre-wrap;word-break:normal;overflow-wrap:anywhere'>{escape(example)}</pre></div>")
    # The facts first (where to send), then the rules and the example; the
    # second and third paragraphs close the email rather than run into the first.
    extra += "<div style='margin-top:22px'>" + "".join(
        f"<p style='{para}'>{escape(t)}</p>" for t in more) + "</div>"
    html = notification_email_html(title, intro, facts, link=None, action=None,
                                   footer="Sent by Kavachio Bordereau Management.",
                                   extra_html=extra)
    text = (_email_text(title, body, facts, None)
            + f"\n\n{rules_title}\n"
            + "\n".join(f"- {w}: {r} — {n}" for w, r, n in rules)
            + "\n\nExample\n" + example)
    return {"subject": title, "html": html, "text": text,
            # A reply carrying a file lands where files are collected.
            "reply_to": send_to if route.channel == "email" else None}


def send(s, route, *, carrier: str, send_to: Optional[str],
         api_key: Optional[str] = None, api_base: Optional[str] = None) -> dict:
    """Email the guide to the broker in the background. Returns who it is going
    to, and whether broker emails are switched on at all. An API route is only
    emailed with a key — when one is minted."""
    from submission_service import notifications_enabled
    if route.channel not in _CHANNEL_WORD or (route.channel == "api" and not api_key):
        return {"recipients": [], "sending": False}
    rcpts = recipients(s, route)
    on = notifications_enabled()
    if not rcpts or not on:
        return {"recipients": rcpts, "sending": False}
    msg = build(s, route, carrier=carrier, send_to=send_to,
                api_key=api_key, api_base=api_base)
    tenant_id, route_id = route.tenant_id, route.id

    def _run():
        from audit import log_activity
        from email_utils import send_email
        from submission_service import _blocked_by_test_mode
        for r in rcpts:
            if _blocked_by_test_mode(r):
                status, err = "skipped", "test mode: address not in MAIL_ALLOWED_RECIPIENTS"
            else:
                try:
                    send_email(r, msg["subject"], msg["html"], text=msg["text"],
                               account="NOTIFY", reply_to=msg["reply_to"])
                    status, err = "sent", None
                except Exception as exc:  # noqa: BLE001 — recorded, and resendable
                    status, err = "failed", str(exc)[:300]
                    log.info("channel guide for route %s to %s failed: %s", route_id, r, exc)
            try:
                log_activity(tenant_id, "Kavachio", "intake_guide_emailed",
                             target=str(route_id),
                             details={"recipient": r, "status": status, "error": err})
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=_run, name=f"intake-guide-{route_id}", daemon=True).start()
    return {"recipients": rcpts, "sending": True}
