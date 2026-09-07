"""Feature 10 — the landing pipeline shared by every way in.

The whole point of this module is one function, `land_file`. Whichever door a
file used — SFTP folder, email attachment, machine POST, someone dragging it
onto a screen — it lands here, gets the same checks and produces one
`file_arrival` row. The door only changes how it got here; it never changes what
happens next.

That matters because the three headless channels (SFTP, email, API) have no
human to say which carrier and which programme a file is for. They cannot reuse
/direct/run, which takes both as form fields chosen on screen. The route the
file arrived through IS the identity: the folder it was dropped in belongs to
one broker, and the broker belongs to the carrier.

Deliberately NOT here: handing an accepted file to the processing pipeline. That
is a separate step (see `land_file`'s closing note) and wiring it now would
change how existing uploads behave, which this feature must not do.

Nothing in this module is imported by existing code paths.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import func

import intake_required_fields as required_fields
import intake_safety as safety
from intake_models import FileArrival, IntakeRoute

log = logging.getLogger("kavachio.intake")

# Crockford base32 (no I, L, O, U — the characters people mis-read aloud).
_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_public_ref() -> str:
    """`inb_01M1GMSW5MKP01R` — time-ordered so references sort by arrival, and
    random enough that nobody can enumerate another carrier's submissions."""
    import secrets, time
    ms, head = int(time.time() * 1000), ""
    for _ in range(10):
        ms, rem = divmod(ms, 32)
        head = _B32[rem] + head
    return "inb_" + head + "".join(secrets.choice(_B32) for _ in range(5))

# The file types a bordereau can arrive as. Anything else is not a spreadsheet
# and cannot be read row-by-row, whatever its name says.
ACCEPTED_EXTENSIONS = (".xlsx", ".xlsm", ".xls", ".csv", ".xml", ".json")

# First bytes that prove what a file really is, regardless of extension. A PDF
# renamed to .xlsx is still a PDF, and the extension is the one thing a broker's
# export script gets wrong most often.
_MAGIC = {
    b"PK\x03\x04": "xlsx",          # any zip — xlsx/xlsm are zips
    b"\xd0\xcf\x11\xe0": "xls",     # OLE2 compound document — legacy Excel
    b"%PDF": "pdf",                 # named so the refusal can say what it IS
}

# The checks, in the order they run. Cheapest and most certain first, so a file
# that was never going to work is refused in the first second rather than an
# hour into processing.
#
# ORDER IS SAFETY, NOT JUST SPEED (12.1). Everything above `can_open` is settled
# WITHOUT parsing the file. That matters because opening a spreadsheet is the
# one step that executes a stranger's choices — openpyxl, pandas and the XML
# parser all run on bytes we did not write. Until this order existed, that step
# ran FIRST, before every check meant to protect it.
#
# NOTE on outcomes: 'held' is not a refusal. The file is fine, a person just has
# to make a call — a suspected duplicate, an empty file, a changed layout, no
# live contract yet. Migration 10_2 made it a real value; before that these
# landed as `turned_away` with a reason beginning "Held —".
CHECKS = (
    ("size",             "turned_away", "Is it small enough to read?"),
    ("is_spreadsheet",   "turned_away", "Is it a spreadsheet at all?"),
    ("safe_to_open",     "turned_away", "Is it safe to open?"),
    ("known_sender",     "turned_away", "Do we know who sent it?"),
    ("not_duplicate",    "held",        "Is it the same file we already have?"),
    ("malware",          "turned_away", "Does it pass the security scan?"),
    ("can_open",         "turned_away", "Can we open it?"),
    ("has_rows",         "held",        "Does it have any rows in it?"),
    ("required_columns", "held",        "Does it have the columns we need?"),
    ("live_contract",    "held",        "Is there a live contract to check it against?"),
)


# ── configuration ───────────────────────────────────────────────────────────
# Read at call time, not import time: this module is imported before main.py
# runs load_dotenv(), so reading at import would miss the .env entirely (the
# same reason storage.py resolves its config lazily).

def sftp_root() -> Path:
    """The directory the SFTP server drops files into.

    This is a plain filesystem path on purpose. Whatever serves SFTP in front of
    it — a self-hosted sshd chrooted here, or an Azure Blob SFTP mount — the
    collector below only ever sees folders and files, so swapping the transport
    later changes nothing in this file.
    """
    return Path(os.getenv("SFTP_ROOT", "./sftp-root")).expanduser().resolve()


def sftp_host() -> str:
    """Hostname shown to brokers. Config, not data — which is why the address
    stored on a route is the path alone."""
    return os.getenv("SFTP_HOST", "ingest.kavachio.app").strip()


def quiet_seconds() -> int:
    """How long a file must sit untouched before we believe it is complete.

    A file being uploaded looks exactly like a finished one — it is just shorter.
    Taking it early gives you half a bordereau and nobody notices until the
    numbers are wrong. Rather than tracking size snapshots between polls (state
    that does not survive a restart), we simply require that nothing has been
    written to it for this many seconds.
    """
    try:
        return max(1, int(os.getenv("SFTP_QUIET_SECONDS", "30")))
    except ValueError:
        return 30


# ── addresses ───────────────────────────────────────────────────────────────

def slugify(value: str) -> str:
    """A short, safe folder name from a company name.

    Folder names end up in an SFTP path a broker types, so they must survive
    accents, punctuation and spacing: "Halstead & Co" -> "halstead-co".
    """
    text = unicodedata.normalize("NFKD", value or "")
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "broker"


def build_sftp_address(carrier_name: str, broker_name: str) -> str:
    """The path half of a broker's SFTP address: "insurisk/corvin".

    One folder per broker, never one shared folder per carrier. A shared folder
    means working out who sent what from the filename, which is exactly the
    guesswork a route is supposed to remove.
    """
    return f"{slugify(carrier_name)}/{slugify(broker_name)}"


def display_address(route: IntakeRoute) -> str:
    """What the screen shows. Composed at read time so moving hosts does not
    strand every stored address."""
    if route.channel == "sftp":
        return f"sftp://{sftp_host()}/{route.address}"
    return route.address


def route_dirs(route: IntakeRoute) -> tuple[Path, Path, Path]:
    """(incoming, processed, rejected) for one route — the three that existed
    first. `held` and `quarantine` are siblings, resolved by name below, so this
    signature and every caller of it stay as they were."""
    base = sftp_root() / route.address
    return base / "incoming", base / "processed", base / "rejected"


# The five folders a route has, and why each one is separate (12.3):
#
#   incoming    what the broker drops in.
#   processed   accepted, already loaded, must never be read twice.
#   rejected    genuinely bad — a PDF, a corrupt workbook, an unknown sender.
#               The broker has to fix something and send it again.
#   held        NOT bad. A file waiting on a person: a suspected duplicate, a
#               layout that changed, no live contract yet. Mixing these into
#               `rejected` buried the only pile anybody has to act on inside the
#               pile nobody does.
#   quarantine  failed the security scan. Nobody is invited to browse this one.
ROUTE_FOLDERS = ("incoming", "processed", "rejected", "held", "quarantine")


def route_dir(route: IntakeRoute, which: str) -> Path:
    """One named folder for a route. `which` is one of ROUTE_FOLDERS."""
    return sftp_root() / route.address / which


def ensure_route_dirs(route: IntakeRoute) -> Path:
    """Create a route's folders. Safe to call repeatedly, and called on every
    collection so a route created before `held`/`quarantine` existed grows them
    the first time it is polled rather than needing a backfill."""
    base = sftp_root() / route.address
    for name in ROUTE_FOLDERS:
        (base / name).mkdir(parents=True, exist_ok=True)
    return base


def is_quiet(path: Path, now: Optional[datetime] = None) -> bool:
    """True when nothing has been written to `path` for `quiet_seconds`.

    An empty file is never quiet: a zero-byte file is almost always an upload
    that has only just started, not a real (if useless) file. Letting it through
    would refuse it as "no rows" and the broker would be told their good file was
    empty.
    """
    try:
        st = path.stat()
    except OSError:
        return False
    if st.st_size == 0:
        return False
    now = now or datetime.now(timezone.utc)
    age = now - datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    return age >= timedelta(seconds=quiet_seconds())


# ── reading a file just enough to check it ──────────────────────────────────

def _sniff(file_bytes: bytes) -> Optional[str]:
    for magic, kind in _MAGIC.items():
        if file_bytes.startswith(magic):
            return kind
    return None


def count_rows(filename: str, file_bytes: bytes) -> Optional[int]:
    """Total data rows across the file, or None when it cannot be read.

    None and 0 mean different things and both matter: None is "we cannot open
    this" (a refusal), 0 is "we opened it and it is empty" (a hold, because an
    empty file is usually an export that failed silently rather than a bad file).

    Header rows are subtracted for tabular formats — a file containing only
    headers has no business in it and should read as empty, not as one row.
    """
    ext = Path(filename).suffix.lower()
    try:
        if ext in (".xlsx", ".xlsm"):
            from openpyxl import load_workbook
            wb = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
            try:
                return sum(max(0, (ws.max_row or 0) - 1) for ws in wb.worksheets)
            finally:
                wb.close()
        if ext == ".xls":
            import pandas as pd
            sheets = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None)
            return sum(len(df) for df in sheets.values())
        if ext == ".csv":
            text = file_bytes.decode("utf-8-sig", errors="strict")
            rows = list(csv.reader(io.StringIO(text)))
            return max(0, len(rows) - 1)
        if ext == ".json":
            payload = json.loads(file_bytes.decode("utf-8-sig"))
            if isinstance(payload, list):
                return len(payload)
            if isinstance(payload, dict):
                # A wrapped list — {"policies": [...]} — is the common shape.
                for value in payload.values():
                    if isinstance(value, list):
                        return len(value)
                return 1 if payload else 0
            return 0
        if ext == ".xml":
            # Hardened parse: entity declarations and external references are
            # refused outright, so a "billion laughs" file cannot exhaust
            # memory and no document can ask us to read a file off the server.
            root = safety.safe_xml_root(file_bytes)
            return len(list(root))
    except Exception as exc:
        log.info("count_rows failed for %s: %s", filename, exc)
        return None
    return None


# ── the checks ──────────────────────────────────────────────────────────

def _check_is_spreadsheet(filename: str, file_bytes: bytes) -> Optional[str]:
    ext = Path(filename).suffix.lower()
    if ext not in ACCEPTED_EXTENSIONS:
        return (f"It is a {ext or 'file with no extension'}, not a spreadsheet. "
                f"We can read {', '.join(ACCEPTED_EXTENSIONS)}.")
    kind = _sniff(file_bytes)
    if kind == "pdf":
        return "It is a PDF, not a spreadsheet. We cannot read rows out of it."
    # An .xlsx that is not a zip is not an xlsx, whatever it is called.
    if ext in (".xlsx", ".xlsm") and kind != "xlsx":
        return "The file is named .xlsx but its contents are not an Excel workbook."
    return None


def _check_can_open(rows: Optional[int]) -> Optional[str]:
    if rows is None:
        return ("We could not open it. Half-uploaded and password-protected "
                "files look fine until you try to read them.")
    return None


# What a route IS, per channel, so a refusal reads correctly whichever way the
# file came in. "That folder is not linked to a broker" is right for SFTP and
# nonsense for an email — and the sender is exactly who reads these.
_WAY_IN_NOUN = {
    "sftp": "folder", "email": "email address", "api": "key",
    "upload": "login", "cloud_folder": "shared folder",
}


def _check_known_sender(route: Optional[IntakeRoute]) -> Optional[str]:
    if route is None:
        # No route matched, so there is no channel to name — say the thing that
        # is true of all five ways in rather than guessing at one of them.
        return ("We do not recognise the sender. Every file has to come from a "
                "broker on one of your programmes.")
    if route.broker_party_id is None:
        noun = _WAY_IN_NOUN.get(route.channel, "way in")
        return (f"That {noun} is not linked to a broker yet, so there is "
                f"nothing to check the file against.")
    if not route.is_enabled:
        return ("That way in has been switched off. Anything sent this way is "
                "turned away with a note.")
    return None


def _check_not_duplicate(session, tenant_id: int, sha: str,
                         route: Optional[IntakeRoute]) -> Optional[str]:
    """A file we have loaded before, arriving again.

    Only a PREVIOUSLY ACCEPTED file counts. Matching against refused arrivals
    too would mean a broker who fixes nothing and resends gets "duplicate"
    instead of the real reason their file was refused.
    """
    prior = (session.query(FileArrival)
             .filter(FileArrival.tenant_id == tenant_id,
                     FileArrival.file_hash_sha256 == sha,
                     FileArrival.outcome == "accepted")
             .order_by(FileArrival.id.desc()).first())
    if prior is None:
        return None
    when = prior.received_at.strftime("%d %b %Y") if prior.received_at else "earlier"
    return (f"Held — exactly the same file we already loaded on {when}. "
            f"Loading it again would count the premium twice.")


def _check_has_rows(rows: Optional[int]) -> Optional[str]:
    if rows == 0:
        return ("Held — the file opened but has no rows in it. An empty file "
                "usually means an export that failed silently.")
    return None


def _check_live_contract(session, route: Optional[IntakeRoute]) -> Optional[str]:
    """Is there an agreed contract to check this broker's file against?

    Scoped to the broker's programmes rather than to one programme, because a
    route belongs to a broker and `intake_route` has no programme column — see
    the mesh note in land_file.
    """
    if route is None or route.broker_party_id is None:
        return None            # already refused by the sender check
    from db import Contract, ProgramBroker
    q = (session.query(Contract.id)
         .join(ProgramBroker, ProgramBroker.program_id == Contract.program_id)
         .filter(ProgramBroker.broker_party_id == route.broker_party_id,
                 ProgramBroker.status == "active",
                 Contract.status == "active"))
    # When the route names its programme (10.2), check THAT programme's
    # contract rather than "any contract this broker holds anywhere" — which is
    # all the old mesh note could manage without a program_id column.
    program_id = getattr(route, "program_id", None)
    if program_id is not None:
        q = q.filter(Contract.program_id == program_id)
    live = q.first()
    if live is None:
        return ("Held — there is no live contract for this broker yet. There is "
                "nothing to check a file against until the contract is agreed.")
    return None


# ── the landing pipeline ────────────────────────────────────────────────────

def land_file(session, *, tenant_id: int, filename: str, file_bytes: bytes,
              route: Optional[IntakeRoute] = None,
              claimed_sender: Optional[str] = None,
              idempotency_key: Optional[str] = None,
              public_ref: Optional[str] = None,
              blob_ref: Optional[str] = None,
              max_bytes: Optional[int] = None) -> FileArrival:
    """Record one arriving file and decide whether it may go on.

    Always returns a FileArrival — including for a file we refuse. That is the
    point: "Nothing here is lost. A turned-away file is kept exactly as it
    arrived", and the Files Received screen can only show a refusal if a row was
    written for it. The caller commits.

    Programme<->broker is a mesh: one broker can produce into several
    programmes. `intake_route.program_id` (added by migration 10_2) resolves
    that — a route with it set is pinned to one programme, and the live-contract
    check narrows to that programme's contract. A route with it NULL is
    broker-wide, which is how every pre-10.2 SFTP route behaves: the broker is
    known, the programme is not, and the contract check falls back to "any
    active contract this broker holds".

    NOT DONE HERE: handing an accepted file to the processing pipeline. The file
    is checked, stored and recorded; `bdx_upload_id` stays NULL until that seam
    is wired. Doing it here would change how existing uploads behave.
    """
    sha = hashlib.sha256(file_bytes).hexdigest()

    # The row count is LAZY (12.1). Counting rows means opening the file, and
    # opening the file is the thing the first six checks exist to gate. A file
    # refused for its size, its type, a zip that expands to gigabytes, an
    # unknown sender or a virus signature is never opened at all, and its
    # row_count stays NULL — which is the honest record of what happened.
    _counted: list = []

    def rows() -> Optional[int]:
        if not _counted:
            # Bounded: a clean file can still be pathological, and the caller —
            # an API request, a poller loop — must not be stuck behind it.
            _counted.append(safety.with_timeout(
                lambda: count_rows(filename, file_bytes)))
        return _counted[0]

    reason: Optional[str] = None
    for check in (
        # ── settled without opening the file ────────────────────────────────
        lambda: safety.check_size(file_bytes, max_bytes),
        lambda: _check_is_spreadsheet(filename, file_bytes),
        lambda: safety.check_safe_to_open(filename, file_bytes),
        lambda: _check_known_sender(route),
        # Before the scan on purpose: a file we have already accepted has
        # already been scanned, and there is no sense paying for it twice.
        lambda: _check_not_duplicate(session, tenant_id, sha, route),
        lambda: safety.scan_for_malware(filename, file_bytes),
        # ── from here on the file gets opened ───────────────────────────────
        lambda: _check_can_open(rows()),
        lambda: _check_has_rows(rows()),
        lambda: required_fields.check(session, route, filename, file_bytes),
        lambda: _check_live_contract(session, route),
    ):
        reason = check()
        if reason:
            break

    row_count = _counted[0] if _counted else None

    # Four checks mean "hold this for a person to decide" rather than "refuse
    # it": a suspected duplicate, an empty file, a layout missing columns, and
    # no live contract yet. They say so by prefixing their reason with "Held".
    # A held file HAS arrived — it is only waiting on somebody.
    if not reason:
        outcome = "accepted"
    elif reason.startswith("Held"):
        outcome = "held"
    else:
        outcome = "turned_away"

    arrival = FileArrival(
        tenant_id=tenant_id,
        route_id=route.id if route else None,
        matched_broker_party_id=route.broker_party_id if route else None,
        claimed_sender=claimed_sender,
        filename=filename,
        file_size_bytes=len(file_bytes),
        row_count=row_count,
        file_hash_sha256=sha,
        received_at=datetime.now(timezone.utc),
        outcome=outcome,
        turned_away_reason=reason,
        public_ref=public_ref or new_public_ref(),
        idempotency_key=idempotency_key,
        blob_ref=blob_ref,
    )
    session.add(arrival)
    session.flush()

    # THE CALENDAR'S "turned up" MOMENT (requirement 17.2). An accepted file on a
    # route pinned to a programme satisfies that programme's period for this
    # broker — which is the whole point of the calendar knowing when something
    # was due. Only ACCEPTED files count: a turned-away file did not arrive, and
    # a held one has not been decided yet, so neither may tick a deadline off.
    #
    # The period is read from the file NAME, so July's bordereau arriving in
    # September lands on July. When the name says nothing, mark_received falls
    # back to the oldest open period and records that it guessed.
    #
    # Best-effort on purpose: the calendar is a side-feature, and nothing here
    # may stop a file being recorded as arrived.
    if outcome == "accepted" and route is not None and route.program_id is not None:
        try:
            from submission_calendar_service import mark_received
            mark_received(session, route.program_id,
                          received_on=arrival.received_at.date(),
                          broker_party_id=route.broker_party_id,
                          source_filename=filename)
            session.flush()
        except Exception:   # noqa: BLE001
            log.warning("submission-calendar mark_received failed for arrival %s",
                        arrival.public_ref, exc_info=True)

    return arrival


def month_counts(session, tenant_id: int) -> dict[int, int]:
    """{route_id: files this calendar month} — the "This month" column."""
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = (session.query(FileArrival.route_id, func.count(FileArrival.id))
            .filter(FileArrival.tenant_id == tenant_id,
                    FileArrival.received_at >= start,
                    FileArrival.route_id.isnot(None))
            .group_by(FileArrival.route_id).all())
    return {rid: n for rid, n in rows}
