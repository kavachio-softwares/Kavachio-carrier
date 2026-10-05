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

_CHANNEL_WORD = {"email": "Email", "sftp": "SFTP"}


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


def build(s, route, *, carrier: str, send_to: Optional[str]) -> dict:
    """{subject, html, text, reply_to} for one route."""
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
    if route.channel == "email":
        where = "the email subject or the attachment's file name"
        example = (f"From:     {route.address}\n"
                   f"To:       {send_to or ''}\n"
                   f"Subject:  {pcode} - {f'{ccode} - ' if ccode else ''}{month}\n"
                   f"Attach:   the bordereau (.xlsx or .csv)")
    else:
        where = "the file name"
        example = (f"{svc.display_address(route)}/incoming/"
                   f"{pcode}_{f'{ccode}_' if ccode else ''}{ym}.xlsx")

    # What has to be in the subject / file name, and each code to write.
    named = lambda pairs: "; ".join(f"{c} = {n}" for n, c in pairs)   # noqa: E731
    rules = [("Month", "Required", f"e.g. “{month}” or “{ym}”")]
    rules.append(("Programme", "Required" if shared and len(plist) > 1 else "Optional",
                  named([(n, c) for n, c, _ in plist]) or "your programme"))
    for name, _, cs in plist:
        label = f"Contract — {name}" if len(plist) > 1 else "Contract"
        rules.append((label, "Required" if len(cs) > 1 else "Optional",
                      named(cs) if cs else "no live contract yet"))

    how = "by email" if route.channel == "email" else "by SFTP"
    title = f"How to send your bordereaux to {carrier}"
    intro = (f"{carrier} has set up a way for you to send your bordereaux {how}"
             + (f" for {prog}." if not shared and plist else "."))
    more = [f"Send each file as in the example above. We read the month, programme and "
            f"contract from {where}. You can use the code or the full name — the code is "
            f"shorter and avoids typing mistakes. A file without what is required is turned "
            f"away with the reason.",
            "After each file you will get an email with the result. If it has exceptions, "
            "the email includes a secure link to resolve them."]
    body = "\n\n".join([intro, *more])
    facts = [("Channel", _CHANNEL_WORD.get(route.channel, route.channel)),
             ("Send to" if route.channel == "email" else "Upload to",
              send_to if route.channel == "email" else f"{svc.display_address(route)}/incoming"),
             ("Send from", route.address if route.channel == "email" else None),
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
        "<p style='font-size:13px;font-weight:600;color:#111827;margin:0 0 6px'>Example</p>"
        "<pre style='margin:0 0 18px;padding:12px 14px;background:#F4F6FA;border:1px solid #E5E8EE;"
        "border-radius:8px;font:12px/1.6 Menlo,Consolas,monospace;color:#0E1320;"
        f"white-space:pre-wrap;word-break:normal;overflow-wrap:anywhere'>{escape(example)}</pre>"
        f"<p style='font-size:13px;font-weight:600;color:#111827;margin:0 0 6px'>"
        f"What {escape(where)} must say — code or name</p>"
        "<table role='presentation' style='border-collapse:collapse;width:100%;"
        f"border:1px solid #E5E8EE;border-radius:8px'>{rows}</table></div>")
    # The facts first (where to send), then the example and the rules; the
    # second and third paragraphs close the email rather than run into the first.
    extra += "<div style='margin-top:22px'>" + "".join(
        f"<p style='{para}'>{escape(t)}</p>" for t in more) + "</div>"
    html = notification_email_html(title, intro, facts, link=None, action=None,
                                   footer="Sent by Kavachio Bordereau Management.",
                                   extra_html=extra)
    text = (_email_text(title, body, facts, None)
            + "\n\nExample\n" + example
            + f"\n\nWhat {where} must say\n"
            + "\n".join(f"- {w}: {r} — {n}" for w, r, n in rules))
    return {"subject": title, "html": html, "text": text,
            # A reply carrying a file lands where files are collected.
            "reply_to": send_to if route.channel == "email" else None}


def send(s, route, *, carrier: str, send_to: Optional[str]) -> dict:
    """Email the guide to the broker in the background. Returns who it is going
    to, and whether broker emails are switched on at all."""
    from submission_service import notifications_enabled
    if route.channel not in _CHANNEL_WORD:
        return {"recipients": [], "sending": False}
    rcpts = recipients(s, route)
    on = notifications_enabled()
    if not rcpts or not on:
        return {"recipients": rcpts, "sending": False}
    msg = build(s, route, carrier=carrier, send_to=send_to)
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
