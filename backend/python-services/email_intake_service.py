"""Feature 10.3 — reading a mailbox and taking the bordereaux out of it.

The parsing half of email intake. `email_poller` does the talking to IMAP; this
module knows what an email IS: who sent it, which attachments are worth keeping,
and which broker it belongs to.

WHY EMAIL IS THE AWKWARD CHANNEL. Every other way in identifies its sender for
free. SFTP has a folder only one broker's key opens. An API call carries a key
only one broker holds. Email has a `From:` header, which is a CLAIM — anyone can
write anything in it. That is why `file_arrival.claimed_sender` is named the way
it is, and why this module is careful to say "claimed" everywhere it means it.

How a message is matched to a route (5 Oct 2026). Brokers email Kavachio's
own intake mailbox — a carrier is never asked to hand over theirs — and must
copy (Cc) their carrier:

  1. THE FROM: ADDRESS — matched against the route's stored sending address.
  2. THE CARRIER IN CC — one of the carrier's own users must be on the To or
     Cc line. It says which carrier the file is for (one broker can send to
     several through the same mailbox), and it is the bar a file has to clear:
     a sender who does not copy the carrier is turned away with the reason.

Neither is authentication. A deployment that needs more should require SPF/DKIM
to pass at the mail server, before the message ever reaches this code — that is
the mail server's job and it does it far better than we could here.

Nothing in this module talks to the network or the database schema beyond
reading routes. It is pure enough to test with a saved .eml file.

Configuration (all read at CALL time, never at import — this module is imported
before main.py runs load_dotenv, the same reason email_utils resolves late):

  IMAP_HOST              mail server            e.g. mail.acceltree.com
  IMAP_PORT              993 (implicit TLS) or 143 (STARTTLS)   default 993
  IMAP_USER              the mailbox to read    e.g. bordereaux@acceltree.com
  IMAP_PASS              its password / app password
  IMAP_FOLDER            folder to watch                        default INBOX
  IMAP_PROCESSED_FOLDER  where a handled message goes           default Processed
  IMAP_SEARCH            IMAP search for "new"                  default UNSEEN

A named account works the same way as email_utils: set INTAKE_IMAP_USER and ask
for account="INTAKE". Anything the prefix does not set falls back to the plain
name, so a second mailbox usually needs two variables.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import Message
from email.policy import default as default_policy
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Optional

import intake_service as svc
from email_utils import _env
from intake_models import IntakeRoute

log = logging.getLogger("kavachio.intake.email")


# ── configuration ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MailboxConfig:
    """One resolved IMAP identity: where to connect and what to read."""
    host: str
    port: int
    user: str
    password: str
    folder: str
    processed_folder: str
    search: str

    @property
    def use_ssl(self) -> bool:
        """993 is implicit TLS; 143 is plaintext then STARTTLS.

        Both are encrypted by the time credentials cross the wire — the
        difference is only when the handshake happens.
        """
        return self.port == 993


def _intake_account(account: str) -> str:
    """Which mailbox brokers send to. EMAIL_INTAKE_ACCOUNT=NOTIFY makes it the
    Kavachio mailbox the platform already sends as (NOTIFY_SMTP_*)."""
    return (account or os.getenv("EMAIL_INTAKE_ACCOUNT") or "").strip().upper()


def _scoped(prefix: str, *names: str) -> str:
    """The first of <PREFIX>_<name> that is set — never the unprefixed value,
    which belongs to a different mailbox."""
    for n in names:
        v = (os.getenv(f"{prefix}_{n}") or "").strip()
        if v:
            return v
    return ""


def mailbox_config(account: str = "") -> MailboxConfig:
    """Resolve the named IMAP mailbox ("" = the intake one).

    A named account reads its OWN settings only, and its login falls back to
    the same account's SMTP login — the Kavachio mailbox is one login for
    sending and reading. Its server, when not set, is the SMTP server's IMAP
    twin (smtp.gmail.com → imap.gmail.com)."""
    prefix = _intake_account(account)
    if prefix:
        smtp_host = _scoped(prefix, "SMTP_HOST") or (os.getenv("SMTP_HOST") or "").strip()
        twin = "imap." + smtp_host[5:] if smtp_host.startswith("smtp.") else ""
        return MailboxConfig(
            host=_scoped(prefix, "IMAP_HOST") or twin,
            port=int(_scoped(prefix, "IMAP_PORT") or 993),
            user=_scoped(prefix, "IMAP_USER", "SMTP_USER"),
            password=_scoped(prefix, "IMAP_PASS", "SMTP_PASS").replace(" ", ""),
            folder=_env(prefix, "IMAP_FOLDER", "INBOX"),
            processed_folder=_env(prefix, "IMAP_PROCESSED_FOLDER", "Processed"),
            search=_env(prefix, "IMAP_SEARCH", "UNSEEN"),
        )
    return MailboxConfig(
        host=_env(prefix, "IMAP_HOST"),
        port=int(_env(prefix, "IMAP_PORT", "993") or 993),
        user=_env(prefix, "IMAP_USER"),
        # Providers show app passwords in four space-separated groups; the
        # spaces are presentation only, exactly as email_utils treats SMTP_PASS.
        password=_env(prefix, "IMAP_PASS").replace(" ", ""),
        folder=_env(prefix, "IMAP_FOLDER", "INBOX"),
        processed_folder=_env(prefix, "IMAP_PROCESSED_FOLDER", "Processed"),
        search=_env(prefix, "IMAP_SEARCH", "UNSEEN"),
    )


def is_configured(account: str = "") -> bool:
    """True when there is enough config to attempt a connection.

    Checked before connecting so an unconfigured deployment logs one clear line
    instead of an imaplib traceback every poll.
    """
    cfg = mailbox_config(account)
    return bool(cfg.host and cfg.user and cfg.password)


# ── addresses ───────────────────────────────────────────────────────────────

def normalise_addr(value: Optional[str]) -> str:
    """Lowercased bare address: "Sachin <A@B.com>" -> "a@b.com".

    Matching is case-insensitive because mail addresses are, in practice, and a
    broker whose signature capitalises their own address should not become an
    unknown sender.
    """
    if not value:
        return ""
    pairs = getaddresses([value])
    addr = pairs[0][1] if pairs else value
    return (addr or "").strip().strip("<>").lower()


def _decode(value: Optional[str]) -> str:
    """A header as text, whatever encoding it arrived in."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


# ── parsing ─────────────────────────────────────────────────────────────────

# Attachments that are part of the message furniture rather than the point of
# it. Filtering by extension alone is not enough: a signature logo is often
# called image001.png, but a spreadsheet is never inline.
_FURNITURE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".ico", ".webp",
    ".vcf", ".ics", ".p7s", ".asc", ".sig",
}

# Headers that mark a message as machine-generated. Replying to one of these is
# how a mail loop starts, and a loop between two auto-responders will get the
# domain blacklisted long before anyone notices.
_AUTOMATED_FROM = re.compile(
    r"(^|[.\-_])(no[-_.]?reply|donotreply|mailer[-_.]?daemon|postmaster|"
    r"bounces?|notifications?)([.\-_]|@)", re.I)


@dataclass
class Attachment:
    filename: str
    content: bytes

    @property
    def size(self) -> int:
        return len(self.content)


@dataclass
class ParsedMessage:
    """One email, reduced to the parts intake cares about."""
    message_id: str
    from_addr: str
    from_display: str
    to_addrs: list[str]
    subject: str
    date: Optional[object] = None
    attachments: list[Attachment] = field(default_factory=list)
    is_automated: bool = False

    @property
    def has_files(self) -> bool:
        return bool(self.attachments)


def _is_automated(msg: Message, from_addr: str) -> bool:
    """Would replying to this message risk a loop?

    Three independent signals, any one of which is enough. Being wrong in the
    cautious direction costs a broker one notification; being wrong the other
    way costs the domain its reputation.
    """
    auto = (msg.get("Auto-Submitted") or "").strip().lower()
    if auto and auto != "no":
        return True
    precedence = (msg.get("Precedence") or "").strip().lower()
    if precedence in ("bulk", "list", "junk", "auto_reply"):
        return True
    if msg.get("List-Id") or msg.get("List-Unsubscribe"):
        return True
    return bool(_AUTOMATED_FROM.search(from_addr or ""))


def _wanted(filename: str, part: Message) -> bool:
    """Is this attachment plausibly a bordereau?

    Deliberately generous on TYPE and strict on ROLE: the six checks in
    intake_service are the authority on whether a file is really a spreadsheet,
    and duplicating that judgement here would mean two places to fix. All this
    decides is "was this attached on purpose?".
    """
    ext = Path(filename).suffix.lower()
    if not ext or ext in _FURNITURE_EXTENSIONS:
        return False
    # An inline part is displayed within the message body — a logo, a pasted
    # screenshot. Nobody sends a bordereau inline.
    disposition = (part.get_content_disposition() or "").lower()
    if disposition == "inline":
        return False
    # A Content-ID usually means the HTML body points at the part. But Gmail
    # gives EVERY attachment one (with X-Attachment-Id), so an explicit
    # "attachment" disposition wins — otherwise every bordereau sent from Gmail
    # was dropped as furniture and the message merely marked read (5 Oct 2026).
    if part.get("Content-ID") and disposition != "attachment":
        return False                      # referenced by the HTML body
    return True


def parse_message(raw: bytes) -> ParsedMessage:
    """One raw RFC822 message -> the bits we act on.

    Uses the modern email policy so encoded filenames ("=?utf-8?B?…?=" and
    RFC 2231 continuations) come back as the text a person would recognise — a
    broker sending "bordereau août.xlsx" should not become a mystery file.
    """
    try:
        msg = message_from_bytes(raw, policy=default_policy)
    except Exception:
        # A message we cannot even frame is still worth a From: guess, so the
        # poller can say who it was from rather than dropping it silently.
        msg = message_from_bytes(raw)

    from_raw = msg.get("From", "")
    from_addr = normalise_addr(from_raw)
    recipients: list[str] = []
    for header in ("To", "Cc", "Delivered-To", "X-Original-To", "Envelope-To"):
        for _, addr in getaddresses(msg.get_all(header, []) or []):
            norm = normalise_addr(addr)
            if norm and norm not in recipients:
                recipients.append(norm)

    when = None
    try:
        when = parsedate_to_datetime(msg.get("Date")) if msg.get("Date") else None
    except Exception:
        when = None

    parsed = ParsedMessage(
        message_id=(msg.get("Message-ID") or "").strip().strip("<>"),
        from_addr=from_addr,
        from_display=_decode(from_raw),
        to_addrs=recipients,
        subject=_decode(msg.get("Subject")),
        date=when,
        is_automated=_is_automated(msg, from_addr),
    )

    seen: set[str] = set()
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = _decode(part.get_filename())
        if not filename or not _wanted(filename, part):
            continue
        try:
            content = part.get_payload(decode=True)
        except Exception as exc:
            log.warning("could not decode attachment %s: %s", filename, exc)
            continue
        if not content:
            continue
        # Two attachments with the same name in one message: keep both, but make
        # the names distinct so Files Received does not show two identical rows.
        name = Path(filename).name or "attachment"
        if name in seen:
            stem, suffix = Path(name).stem, Path(name).suffix
            n = 2
            while f"{stem} ({n}){suffix}" in seen:
                n += 1
            name = f"{stem} ({n}){suffix}"
        seen.add(name)
        parsed.attachments.append(Attachment(filename=name, content=content))

    return parsed


# ── matching a message to a broker ──────────────────────────────────────────

def carrier_addresses(session, tenant_id: int) -> set[str]:
    """The carrier's own users' addresses — any of them in To/Cc will do."""
    from submission_calendar_service import carrier_contacts
    return {normalise_addr(c.get("email")) for c in
            carrier_contacts(session, [tenant_id]).get(tenant_id, []) if c.get("email")}


def carrier_cc(session, tenant_id: int) -> Optional[str]:
    """The address a broker is told to copy: the carrier's best contact."""
    from submission_service import _carrier_email
    return _carrier_email(session, tenant_id)


def resolve_route(session, tenant_id: Optional[int],
                  parsed: ParsedMessage) -> tuple[Optional[IntakeRoute], str, Optional[str]]:
    """Which broker's route does this message belong to?

    Returns (route, how, refusal). `refusal` is set when the sender is known
    but did not copy their carrier: the file is recorded on that route and
    turned away with that reason.

    `tenant_id=None` searches every tenant's email routes. One mailbox can serve
    several carriers, and a message does not say which one it is for — the route
    it matches is what decides, and the route carries the tenant.
    """
    q = (session.query(IntakeRoute)
         .filter(IntakeRoute.channel == "email"))
    if tenant_id is not None:
        q = q.filter(IntakeRoute.tenant_id == tenant_id)
    routes = q.order_by(IntakeRoute.id).all()
    if not routes:
        return None, "no email routes configured", None

    # 1. The From: header — whose sending address is it? One route per carrier.
    mine = [r for r in routes
            if parsed.from_addr and normalise_addr(r.address) == parsed.from_addr]
    if not mine:
        return None, "no route matches the sender", None

    # 2. Which of those carriers is copied on the email.
    to = set(parsed.to_addrs)
    copied = [r for r in mine if carrier_addresses(session, r.tenant_id) & to]
    if len(copied) == 1:
        return copied[0], "From: address, carrier copied", None
    if len(copied) > 1:
        return None, "more than one of the sender's carriers is copied", None
    if len(mine) == 1:
        r = mine[0]
        cc = carrier_cc(session, r.tenant_id)
        return r, "From: address, carrier not copied", (
            "Not accepted — the email did not copy the carrier. Send it again with "
            f"{cc or 'your carrier'} in Cc.")
    return None, "the sender's carrier is not copied", None


def build_email_address(broker_sending_address: str) -> str:
    """The stored address for an email route.

    It is the broker's SENDING address, not the mailbox they send to. Every
    broker emails the same inbox, so storing that would give every route the
    same address and the second one would collide on
    UNIQUE (tenant_id, channel, address). What actually differs between email
    routes — and what identifies the sender — is who the mail comes from.
    """
    addr = normalise_addr(broker_sending_address)
    if not addr or "@" not in addr:
        raise ValueError("a broker's email address is needed for an email route")
    return addr
