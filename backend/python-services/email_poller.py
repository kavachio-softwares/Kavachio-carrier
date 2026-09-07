"""Feature 10.3 — the collector that takes bordereaux out of a mailbox.

The email twin of sftp_poller. It logs into one IMAP mailbox every few minutes,
takes the attachments off anything new, hands each one to
intake_service.land_file, and moves the message out of the way so it is never
read twice.

It talks to IMAP with the standard library — `imaplib` and `email`, no new
dependency. Whatever hosts the mailbox (Dovecot, an appliance, a provider that
allows password auth) this file does not change.

WHAT IS DIFFERENT FROM SFTP, and why the two collectors are not one function:

  * ONE MAILBOX, MANY BROKERS. An SFTP route owns a folder; every email route
    shares one inbox. So the advisory lock is on the MAILBOX, not the route, and
    a poll collects everything — it cannot collect "just this broker's" mail.
  * NO QUIET PERIOD. A half-written file is a real hazard on a filesystem; IMAP
    does not show a message until the server has all of it.
  * ONE MESSAGE, MANY FILES. Each attachment becomes its own arrival.
  * THERE IS SOMEONE TO REPLY TO. Email is the first way in with a return path,
    so a refusal can actually reach the person who sent it. Off by default —
    see EMAIL_REPLY_ON_REFUSAL below.

OFF BY DEFAULT. Set EMAIL_POLLER_ENABLED=1 to run it. Nothing in the app behaves
differently until you do, and "Collect now" on the screen works either way.

Configuration:
  EMAIL_POLLER_ENABLED    0/1   (default 0 — off)
  EMAIL_POLL_SECONDS      int   (default 300 — the design's "every 5 minutes")
  EMAIL_MAX_ATTACHMENT_MB int   (default 25)  skip anything larger
  EMAIL_REPLY_ON_REFUSAL  0/1   (default 0 — off) tell the sender we refused it
  EMAIL_REPLY_MAX_PER_DAY int   (default 3)   per sender, a loop-breaker
  IMAP_*                        see email_intake_service.mailbox_config
"""
from __future__ import annotations

import asyncio
import imaplib
import logging
import os
import ssl
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

import email_intake_service as mail
import intake_service as svc
import storage
from db import SessionLocal
from intake_models import FileArrival, IntakeRoute

log = logging.getLogger("kavachio.email_poller")

_task: asyncio.Task | None = None

# Namespace for the Postgres advisory lock, distinct from sftp_poller's so the
# two collectors can never block each other.
_LOCK_NAMESPACE = 0x4D41  # "MA" — mail

# imaplib refuses very large literals by default; a bordereau with a year of
# policies can legitimately be tens of megabytes.
imaplib._MAXLINE = max(getattr(imaplib, "_MAXLINE", 10000), 10_000_000)


def _enabled() -> bool:
    return os.getenv("EMAIL_POLLER_ENABLED", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _interval() -> int:
    try:
        return max(10, int(os.getenv("EMAIL_POLL_SECONDS", "300")))
    except ValueError:
        return 300


def _max_bytes() -> int:
    try:
        return max(1, int(os.getenv("EMAIL_MAX_ATTACHMENT_MB", "25"))) * 1024 * 1024
    except ValueError:
        return 25 * 1024 * 1024


def _reply_on_refusal() -> bool:
    return os.getenv("EMAIL_REPLY_ON_REFUSAL", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _reply_cap() -> int:
    try:
        return max(0, int(os.getenv("EMAIL_REPLY_MAX_PER_DAY", "3")))
    except ValueError:
        return 3


# ── IMAP plumbing ───────────────────────────────────────────────────────────

def connect(cfg: mail.MailboxConfig) -> imaplib.IMAP4:
    """Log in and select the folder. Raises on failure — the caller reports it.

    Port 993 is implicit TLS; 143 is plaintext upgraded by STARTTLS. The
    credential never crosses the wire unencrypted either way, and a server that
    refuses STARTTLS on 143 fails here rather than silently sending the password
    in the clear.
    """
    context = ssl.create_default_context()
    if cfg.use_ssl:
        imap = imaplib.IMAP4_SSL(cfg.host, cfg.port, ssl_context=context)
    else:
        imap = imaplib.IMAP4(cfg.host, cfg.port)
        imap.starttls(ssl_context=context)
    imap.login(cfg.user, cfg.password)
    imap.select(cfg.folder)
    return imap


def _close(imap: imaplib.IMAP4 | None) -> None:
    """Hang up politely; never let cleanup mask the real error."""
    if imap is None:
        return
    try:
        imap.close()
    except Exception:
        pass
    try:
        imap.logout()
    except Exception:
        pass


def _new_uids(imap: imaplib.IMAP4, search: str) -> list[bytes]:
    """UIDs of messages matching the "new" search, oldest first.

    UIDs, not sequence numbers: a sequence number is only valid until something
    else changes the folder, and this loop deletes as it goes.
    """
    typ, data = imap.uid("SEARCH", None, search)
    if typ != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def _fetch(imap: imaplib.IMAP4, uid: bytes) -> bytes | None:
    typ, data = imap.uid("FETCH", uid, "(RFC822)")
    if typ != "OK" or not data:
        return None
    for part in data:
        if isinstance(part, tuple) and len(part) > 1:
            return part[1]
    return None


def _retire(imap: imaplib.IMAP4, uid: bytes, cfg: mail.MailboxConfig,
            took_something: bool = True) -> None:
    """Put a handled message beyond the reach of the next poll.

    The email equivalent of moving a file into /processed. Preference order:

      1. MOVE into the processed folder — one atomic server-side operation.
      2. COPY then delete — the same thing on a server without RFC 6851.
      3. Mark \\Seen — no processed folder configured, so the default UNSEEN
         search is what keeps it from being read again.

    `took_something=False` means we opened the message and there was nothing in
    it for us — an ordinary email with no attachment. Those are marked read and
    LEFT WHERE THEY ARE. Filing them away is right for a dedicated intake
    mailbox and badly wrong for a shared or personal one, where it would quietly
    empty somebody's inbox into a folder they never asked for. Marking read is
    the least we can do and still not re-download every message on every poll.

    Marking is deliberately the LAST thing that happens to a message: the
    arrival rows are already committed by the time we get here, so a failure
    now costs a re-read (caught by the duplicate check) rather than a file that
    vanished with no record of it.
    """
    if cfg.processed_folder and took_something:
        try:
            imap.create(cfg.processed_folder)   # no-op when it already exists
        except Exception:
            pass
        try:
            typ, _ = imap.uid("MOVE", uid, cfg.processed_folder)
            if typ == "OK":
                return
        except Exception:
            pass                                # server has no MOVE; fall back
        try:
            typ, _ = imap.uid("COPY", uid, cfg.processed_folder)
            if typ == "OK":
                imap.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
                imap.expunge()
                return
        except Exception as exc:
            log.warning("could not file message %s away: %s", uid, exc)
    imap.uid("STORE", uid, "+FLAGS", "(\\Seen)")


# ── telling the sender ──────────────────────────────────────────────────────

def _already_told_today(session, sender: str) -> int:
    since = datetime.now(timezone.utc) - timedelta(days=1)
    return (session.query(FileArrival)
            .filter(FileArrival.claimed_sender == sender,
                    FileArrival.sender_notified_at.isnot(None),
                    FileArrival.sender_notified_at >= since).count())


def notify_sender(session, arrival: FileArrival,
                  parsed: mail.ParsedMessage) -> bool:
    """Tell the sender we could not use their file. Returns True if we did.

    Email is the ONLY way in with a reply path — an SFTP folder has nobody to
    tell, which is why Files Received says exactly that. So this is where that
    column finally gets a real value.

    Four guards, because an auto-responder answering an auto-responder is how a
    domain ends up on a blocklist:

      * only for `turned_away` — a `held` file is waiting on a PERSON, and a
        machine saying "we could not use this" would be wrong and alarming;
      * never to automated senders (Auto-Submitted, Precedence: bulk, no-reply);
      * never more than EMAIL_REPLY_MAX_PER_DAY times to one sender;
      * off entirely unless EMAIL_REPLY_ON_REFUSAL is set.
    """
    if not _reply_on_refusal():
        return False
    if arrival.outcome != "turned_away":
        return False
    if parsed.is_automated or not parsed.from_addr:
        return False
    if _already_told_today(session, parsed.from_addr) >= _reply_cap():
        log.info("not replying to %s — daily cap reached", parsed.from_addr)
        return False

    try:
        from email_utils import send_email
        reason = arrival.turned_away_reason or "We could not read the file."
        send_email(
            to=parsed.from_addr,
            subject=f"We could not use “{arrival.filename}”",
            html=(
                f"<p>Thank you for sending <b>{arrival.filename}</b>.</p>"
                f"<p>We were not able to use it:</p>"
                f"<blockquote>{reason}</blockquote>"
                f"<p>Nothing has been lost — the file is kept exactly as it "
                f"arrived. Please reply to this message if you think it should "
                f"have gone through.</p>"
            ),
            text=(f"Thank you for sending {arrival.filename}.\n\n"
                  f"We were not able to use it:\n\n  {reason}\n\n"
                  f"Nothing has been lost — the file is kept exactly as it "
                  f"arrived. Please reply if you think it should have gone "
                  f"through."),
        )
    except Exception as exc:
        # A mail we could not send must not undo a file we did record.
        log.warning("could not tell %s about %s: %s",
                    parsed.from_addr, arrival.filename, exc)
        return False

    arrival.sender_notified_at = datetime.now(timezone.utc)
    arrival.sender_notified_via = "email"
    return True


# ── collecting ──────────────────────────────────────────────────────────────

def _fallback_tenant(session) -> int | None:
    """Whose Files Received should an UNRECOGNISED sender appear on?

    A message from nobody we know still has to be recorded — "Nothing here is
    lost", and a refusal can only be shown if a row exists. But `file_arrival`
    needs a tenant and an unmatched message names none.

    When exactly one tenant has email routes the answer is obvious. When several
    do, there is no honest way to choose, so the message is left untouched and
    logged rather than filed against the wrong carrier.
    """
    tenants = [t for (t,) in session.query(IntakeRoute.tenant_id)
               .filter(IntakeRoute.channel == "email").distinct().all()]
    return tenants[0] if len(tenants) == 1 else None


def _handle_message(session, raw: bytes, summary: dict):
    """Land every usable attachment on one message. Says what to do with it.

    Returns one of three things, because a mailbox holds three kinds of mail and
    they must not be treated alike:

      "took"     a bordereau came off it — file it into the processed folder.
      "nothing"  opened, nothing for us — mark it read and LEAVE IT THERE. On a
                 shared or personal mailbox, moving these would quietly empty
                 somebody's inbox.
      False      we cannot tell who it belongs to — touch nothing at all, so a
                 configuration fix picks it up next time rather than losing it.
    """
    parsed = mail.parse_message(raw)
    route, how = mail.resolve_route(session, None, parsed)

    if not parsed.has_files:
        # A plain message with nothing attached is not a refusal, it is not
        # anything. Recording it would fill Files Received with every
        # out-of-office and newsletter the mailbox receives. Marked read so it
        # is not downloaded again, but never moved — see _retire.
        summary["no_attachment"] += 1
        return "nothing"

    tenant_id = route.tenant_id if route else _fallback_tenant(session)
    if tenant_id is None:
        log.warning("message from %s matches no route and no single tenant "
                    "owns email intake — leaving it in the mailbox",
                    parsed.from_addr or "an unknown sender")
        summary["unattributable"] += 1
        return False

    took = False
    for attachment in parsed.attachments:
        # A crash between landing an attachment and retiring the message would
        # re-read it. The duplicate check would catch that, but it would tell a
        # broker their good file was a duplicate of itself — so make the retry
        # a no-op instead.
        key = f"email:{parsed.message_id}:{attachment.filename}" \
            if parsed.message_id else None
        if key and (session.query(FileArrival)
                    .filter(FileArrival.tenant_id == tenant_id,
                            FileArrival.idempotency_key == key).first()):
            summary["already_seen"] += 1
            took = True          # it IS ours, we just have it already
            continue

        # 12.2 — an attachment over the mail limit used to be dropped here
        # with nothing but a log line: no arrival row, no reason, no reply. The
        # broker sent a file and heard nothing, and we had no record it ever
        # came. It now goes through the same landing as everything else and is
        # refused BY the size check, which writes the row and the reason, and
        # notify_sender tells them. Mail has a tighter cap than the other two
        # doors (servers cap attachments anyway), so it passes its own.
        # 12.3 — keep our OWN copy before deciding anything.
        #
        # SFTP moves the file into a folder we own; the API stores the bytes
        # before it lands them. Email did neither: the row was written and the
        # only copy of the file stayed in the mailbox. Move that message, rotate
        # the mailbox, or let somebody tidy old mail, and the file is gone —
        # so "a refused file is kept exactly as it arrived" was simply not true
        # for one door in three.
        #
        # Stored BEFORE the checks run, deliberately. A file refused for its
        # size or a virus signature is the one you most need to still have.
        blob_ref = None
        try:
            blob_ref, _ = storage.store_or_keep(
                "intake", tenant_id, attachment.filename, attachment.content)
        except Exception as exc:
            # Losing the copy must not lose the RECORD. Better an arrival row
            # with no blob than a file that vanishes with nothing to show it
            # ever came.
            log.error("could not store %s from %s: %s",
                      attachment.filename, parsed.from_addr, exc)

        arrival = svc.land_file(
            session, tenant_id=tenant_id, filename=attachment.filename,
            file_bytes=attachment.content, route=route,
            max_bytes=_max_bytes(), blob_ref=blob_ref,
            # The From: header is a CLAIM, not proof — anyone can write anything
            # in it. Recorded as sent so "who tried?" has an answer even when it
            # matched nobody.
            claimed_sender=parsed.from_addr or None,
            idempotency_key=key,
        )
        notify_sender(session, arrival, parsed)
        session.commit()
        took = True

        summary.setdefault(arrival.outcome, 0)
        summary[arrival.outcome] += 1
        summary["files"].append({
            "filename": attachment.filename,
            "outcome": arrival.outcome,
            "reason": arrival.turned_away_reason,
            "arrival_id": arrival.id,
            "from": parsed.from_addr,
            "matched": how if route else "no route matches the sender",
            "subject": parsed.subject,
        })
    # "took" distinguishes a message we filed a bordereau from — which belongs
    # in the processed folder — from one that merely passed through.
    return "took" if took else "nothing"



def collect_mailbox(session, account: str = "") -> dict:
    """Read the mailbox once. Safe to call repeatedly.

    ORDER MATTERS, exactly as it does for SFTP: parse, record the arrivals,
    COMMIT, and only then retire the message. Retiring first and crashing would
    move a broker's file out of sight with no row anywhere to say it existed.
    Doing it in this order can at worst re-read a message, which the
    idempotency key and the duplicate check both absorb.
    """
    cfg = mail.mailbox_config(account)
    summary: dict = {"mailbox": cfg.user, "folder": cfg.folder,
                     "messages": 0, "accepted": 0, "held": 0, "turned_away": 0,
                     # `too_large` stays in the shape for the screen that reads
                     # it, but is now always 0: since 12.2 an oversized
                     # attachment is a recorded refusal, counted under
                     # turned_away with a reason the sender is told.
                     "no_attachment": 0, "too_large": 0, "already_seen": 0,
                     "unattributable": 0, "files": []}

    if not mail.is_configured(account):
        summary["error"] = ("no mailbox configured — set IMAP_HOST, IMAP_USER "
                            "and IMAP_PASS")
        return summary

    # One worker per MAILBOX — not per route, because every email route shares
    # the same inbox. Two workers reading it at once would land the same
    # attachment twice and double the premium.
    #
    # Taken on a DEDICATED connection inside its own transaction, for the two
    # reasons spelled out in sftp_poller: the session's connection goes back to
    # the pool on every commit below (so a lock held on it would leak), and a
    # transaction-scoped lock is released even if this process dies.
    lock_key = (_LOCK_NAMESPACE << 32) | (hash(cfg.user) & 0xFFFFFFFF)
    lock_conn = session.get_bind().connect()
    try:
        lock_txn = lock_conn.begin()
        got_lock = lock_conn.execute(
            text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": lock_key}).scalar()
    except Exception:
        lock_conn.close()
        raise
    if not got_lock:
        lock_txn.rollback()
        lock_conn.close()
        summary["skipped"] = "another worker is already reading this mailbox"
        return summary

    imap = None
    try:
        try:
            imap = connect(cfg)
        except Exception as exc:
            # Wrong password, server down, folder renamed. Reported rather than
            # raised so "Collect now" can say what happened on the screen.
            summary["error"] = f"could not open the mailbox: {exc}"
            return summary

        for uid in _new_uids(imap, cfg.search):
            raw = _fetch(imap, uid)
            if raw is None:
                continue
            summary["messages"] += 1
            try:
                # False  -> leave it entirely alone, we could not attribute it
                # "took"  -> a bordereau came off it; file it away
                # "nothing" -> opened, nothing for us; mark read, do not move
                handled = _handle_message(session, raw, summary)
                if handled:
                    _retire(imap, uid, cfg, took_something=(handled == "took"))
            except Exception as exc:
                # One bad message must not stop the rest of the mailbox. It is
                # left unread on purpose, so a fix picks it up next time.
                session.rollback()
                log.exception("could not handle message %s: %s", uid, exc)
    finally:
        _close(imap)
        # Ending the transaction is what releases the lock.
        lock_txn.rollback()
        lock_conn.close()

    return summary


def collect_route(session, route: IntakeRoute) -> dict:
    """"Collect now" for one email route.

    Reading a mailbox cannot be narrowed to a single broker — every email route
    shares the inbox — so this collects everything and then says which of the
    files were this route's. The summary is explicit about that rather than
    quietly reporting other brokers' files as if they were this one's.
    """
    summary = collect_mailbox(session)
    mine = [f for f in summary.get("files", [])
            if f.get("from") and mail.normalise_addr(route.address) == f["from"]]
    summary["route_id"] = route.id
    summary["address"] = route.address
    summary["looked_in"] = f"{summary.get('mailbox') or 'mailbox'} / {summary.get('folder')}"
    summary["for_this_route"] = len(mine)
    summary["note"] = ("One mailbox serves every email route, so this collected "
                       "all new mail, not only this broker's.")
    return summary


def collect_all() -> dict:
    """The whole mailbox, for the timer. Safe to run repeatedly."""
    totals = {"messages": 0, "accepted": 0, "held": 0, "turned_away": 0}
    with SessionLocal() as s:
        try:
            result = collect_mailbox(s)
            for key in totals:
                totals[key] += result.get(key, 0)
            if result.get("error"):
                totals["error"] = result["error"]
        except Exception as exc:
            s.rollback()
            log.exception("reading the mailbox failed: %s", exc)
            totals["error"] = str(exc)
        s.commit()
    return totals


async def _loop() -> None:
    from fastapi.concurrency import run_in_threadpool
    interval = _interval()
    cfg = mail.mailbox_config()
    log.info("email poller started — every %ss, mailbox=%s folder=%s",
             interval, cfg.user or "(unset)", cfg.folder)
    while True:
        try:
            totals = await run_in_threadpool(collect_all)
            if totals.get("messages"):
                log.info("email poll: %s", totals)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never let a bad poll kill the loop; the next one retries.
            log.exception("email poll failed: %s", exc)
        await asyncio.sleep(interval)


def start(app) -> None:
    """Attach the poller to the app's startup, the same way sftp_poller does."""
    if not _enabled():
        log.info("email poller disabled (set EMAIL_POLLER_ENABLED=1 to run it)")
        return
    if not mail.is_configured():
        log.warning("email poller enabled but no mailbox configured — "
                    "set IMAP_HOST, IMAP_USER and IMAP_PASS")
        return

    @app.on_event("startup")
    async def _start_email_poller() -> None:      # pragma: no cover - wiring
        global _task
        if _task is None or _task.done():
            _task = asyncio.create_task(_loop())

    @app.on_event("shutdown")
    async def _stop_email_poller() -> None:       # pragma: no cover - wiring
        global _task
        if _task is not None:
            _task.cancel()
            _task = None
