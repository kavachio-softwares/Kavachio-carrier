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

from sqlalchemy import func, or_

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
    ("malware",          "turned_away", "Does it pass the security scan?"),
    ("not_duplicate",    "held",        "Is it the same file we already have?"),
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


def sftp_port() -> int:
    """The port brokers connect to — SFTP_PORT, else 22."""
    try:
        return int(os.getenv("SFTP_PORT", "22"))
    except ValueError:
        return 22


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
        return max(1, int(os.getenv("SFTP_QUIET_SECONDS", "10")))
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


def is_external_sftp(route) -> bool:
    """An SFTP route Kavachio PULLS from someone else's server (sftp_pull), as
    opposed to a folder on our own (sftp_poller). Told apart by the address
    alone — "sftp://user@host:port/folder" against a bare "carrier/broker" path —
    so it needs no database column to answer."""
    return (getattr(route, "channel", None) == "sftp"
            and str(getattr(route, "address", "") or "").startswith("sftp://"))


def display_address(route: IntakeRoute) -> str:
    """What the screen shows. Composed at read time so moving hosts does not
    strand every stored address."""
    if route.channel == "sftp":
        if is_external_sftp(route):
            return route.address          # already the remote server's full address
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
        noun = _WAY_IN_NOUN.get(route.channel, "channel")
        return (f"That {noun} is not linked to a broker yet, so there is "
                f"nothing to check the file against.")
    if not route.is_enabled:
        return ("That channel has been switched off. Files sent through it are "
                "rejected with a note.")
    return None


# ── which programme, contract and period a file is for ─────────────────────
#
# Process Bordereau asks a person three things before a file is run: the
# programme, the contract and the reporting period. A file that arrives by API,
# email or SFTP answers the same three — stated (the API's fields), written in
# the email subject or the file name, or, where only one answer is possible,
# taken as read. A file that leaves one open is refused, and the reason says
# what to name and where. That is what lets a file for the same programme +
# contract + period be the next VERSION of the same submission, however it came
# in (submission_service._match).


def _plain(text) -> str:
    """Words only, lower case — "Risk Mahi-demonity Contract", "risk_mahi
    demonity contract" and "risk-mahi-demonity-contract" all read the same."""
    folded = unicodedata.normalize("NFKD", str(text or "")).lower()
    return " ".join(re.findall(r"[a-z0-9]+", folded))


def programme_ref(prog) -> str:
    """The carrier's own code where there is one, else a slug of the name —
    never the surrogate id (see intake_api_routes)."""
    ref = getattr(prog, "program_ref", None)
    if ref:
        return str(ref)
    return "-".join((prog.name or f"programme-{prog.id}").lower().split())


# ── short codes ─────────────────────────────────────────────────────────────
# A programme or contract name can be long, and one misspelt word in a subject
# line turns a file away. So each has a short code a sender can write instead —
# PRG-7K3QMA, CTR-9XW2AB. Derived from the row id, never stored: a keyed
# shuffle (4-round Feistel over 28 bits) makes it unique and stable without
# exposing the id or its sequence. The alphabet has no 0, 1, I, L, O or U, so a
# code is never misread and can never contain a year or a month ("2026", "09")
# that the period reader would pick up.

_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"          # 30 symbols
_CODE_LEN = 6                                               # 30**6 > 2**28


def _shuffle28(n: int, salt: str) -> int:
    left, right = n >> 14, n & 0x3FFF
    for i in range(4):
        f = int.from_bytes(hashlib.sha256(f"kavachio:{salt}:{i}:{right}".encode())
                           .digest()[:2], "big") & 0x3FFF
        left, right = right, left ^ f
    return (left << 14) | right


def _short_code(n: int, salt: str) -> str:
    if not 0 <= n < (1 << 28):
        return str(n)
    v, out = _shuffle28(n, salt), []
    for _ in range(_CODE_LEN):
        v, r = divmod(v, len(_CODE_ALPHABET))
        out.append(_CODE_ALPHABET[r])
    return "".join(reversed(out))


def programme_code(prog) -> str:
    """The programme's short code for subjects and file names: PRG-XXXXXX.
    Takes the programme or its id."""
    pid = prog if isinstance(prog, int) else prog.id
    return f"PRG-{_short_code(int(pid), 'programme')}"


def contract_code(contract) -> str:
    """The contract's short code for subjects and file names: CTR-XXXXXX."""
    return f"CTR-{_short_code(int(contract.id), 'contract')}"


def _code_forms(code: str) -> tuple[str, str]:
    """"PRG-7K3QMA" as written with and without the dash."""
    return code, code.replace("-", "")


def contract_ref(contract) -> str:
    """A contract as a sender names it: a slug of its name."""
    return "-".join((contract.name or contract.filename
                     or f"contract-{contract.id}").lower().split())


_DOC_EXT = re.compile(r"\.(pdf|docx?|rtf|txt)$", re.I)


def contract_label(contract) -> str:
    """The contract's name as a sender writes it — a contract named after its
    uploaded file ("aug-13-Contract_Demoshield.pdf") is named without the
    ".pdf"; nobody types a file extension into an email subject."""
    name = (contract.name or contract.filename or f"Contract {contract.id}").strip()
    return _DOC_EXT.sub("", name) or name


def broker_programmes(session, tenant_id: int, broker_party_id) -> list:
    """The programmes this broker is on at this carrier, now."""
    from db import Program, ProgramBroker
    if not broker_party_id:
        return []
    return (session.query(Program)
            .join(ProgramBroker, ProgramBroker.program_id == Program.id)
            .filter(ProgramBroker.broker_party_id == broker_party_id,
                    ProgramBroker.status == "active",
                    Program.tenant_id == tenant_id)
            .order_by(Program.name).all())


def live_contracts(session, tenant_id: int, program_id: Optional[int],
                   broker_party_id) -> list:
    """The contracts a file on this programme can be written under: the
    broker's own and the carrier's programme-wide ones — the same rule the
    live-contract check and Process Bordereau's contract list use. Light rows
    (id, name, filename), never the stored PDF."""
    if program_id is None:
        return []
    from db import Contract, Program
    from contract_upload_services.contract_asof import NON_GOVERNING_STATUSES
    q = (session.query(Contract.id, Contract.name, Contract.filename)
         .join(Program, Program.id == Contract.program_id)
         .filter(Program.tenant_id == tenant_id,
                 Contract.program_id == program_id,
                 func.coalesce(Contract.status, "").notin_(NON_GOVERNING_STATUSES)))
    if broker_party_id is not None:
        q = q.filter(or_(Contract.broker_party_id == broker_party_id,
                         Contract.broker_party_id.is_(None)))
    return q.order_by(Contract.id).all()


def reporting_periods(session, tenant_id: int, program_id: Optional[int],
                      broker_party_id) -> list[str]:
    """The reporting periods a file may be for — the list Process Bordereau's
    picker offers: this programme's calendar rows for this broker whose period
    has ended, most recently due first. Empty when there is no calendar."""
    if program_id is None:
        return []
    from db import ExpectedSubmission
    rows = (session.query(ExpectedSubmission.period)
            .filter(ExpectedSubmission.tenant_id == tenant_id,
                    ExpectedSubmission.program_id == program_id,
                    ExpectedSubmission.broker_party_id == broker_party_id,
                    ExpectedSubmission.period_end <= datetime.utcnow().date())
            .order_by(ExpectedSubmission.due_date.desc())
            .limit(24).all())
    return [r.period for r in rows]


def _named_in(text: str, options: list, names) -> list:
    """The options whose name or ref the text mentions. Where one name sits
    inside another ("Property" in "Property Fac"), the longer one wins."""
    hits = []
    for o in options:
        found = [n for n in (_plain(x) for x in names(o)) if n and f" {n} " in f" {text} "]
        if found:
            hits.append((max(len(n) for n in found), o))
    if not hits:
        return []
    best = max(h[0] for h in hits)
    return [o for n, o in hits if n == best]


def _listed(names) -> str:
    names = [n for n in names if n]
    return ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")


def identify(session, route: Optional[IntakeRoute], *, filename: str,
             period: Optional[str] = None, period_hint: Optional[str] = None,
             program_id: Optional[int] = None,
             contract_id: Optional[int] = None) -> tuple[dict, Optional[str]]:
    """Which programme, contract and reporting period a file is for.

    Returns (found, None), or (what was found, the reason it cannot go on).
    `program_id`, `contract_id` and `period` are what the sender STATED (the
    API, or the secure link's corrected data); otherwise the email subject
    (`period_hint`) and the file name are read, and a question with only one
    possible answer is taken as answered.

    A programme with no live contract is not refused here: the live-contract
    check holds that file for the carrier, which is what it has always done.
    """
    found = {"program_id": None, "contract_id": None, "period": None,
             "period_source": None}
    if route is None or route.broker_party_id is None:
        return found, None                        # the sender check refuses it
    tenant, broker = route.tenant_id, route.broker_party_id
    text = _plain(f"{period_hint or ''} {filename or ''}")
    where = {"email": "the email subject or the file name",
             "sftp": "the file name"}.get(route.channel, "the request")

    # ── programme ──
    pid = route.program_id or program_id
    if pid is None:
        progs = broker_programmes(session, tenant, broker)
        if len(progs) == 1:
            pid = progs[0].id
        elif progs:
            named = _named_in(text, progs, lambda p: (programme_ref(p), p.name,
                                                      *_code_forms(programme_code(p))))
            if len(named) != 1:
                return found, (f"Which programme? This broker reports on {len(progs)}: "
                               f"{_listed(f'{p.name} ({programme_code(p)})' for p in progs)}. "
                               f"Name one, or its code, in {where}.")
            pid = named[0].id
        else:
            return found, None          # on no programme: no contract, so it is held
    found["program_id"] = pid
    from db import Program
    prog = session.get(Program, pid)

    # ── contract ──
    cons = live_contracts(session, tenant, pid, broker)
    if contract_id is None:
        # A contract CODE that belongs to another of this broker's programmes
        # contradicts the programme. Refused — never quietly swapped for this
        # programme's only contract, which filed it somewhere nobody named.
        own = {c.id for c in cons}
        for p in broker_programmes(session, tenant, broker):
            other = [c for c in live_contracts(session, tenant, p.id, broker)
                     if p.id != pid and c.id not in own]
            hit = _named_in(text, other, lambda c: _code_forms(contract_code(c)))
            if hit:
                return found, (f"{contract_code(hit[0])} is a contract of {p.name}, not "
                               f"of {prog.name if prog else 'this programme'} "
                               f"({programme_code(prog) if prog else ''}). Check the "
                               f"codes in {where}.")
    if contract_id is not None:
        if contract_id not in {c.id for c in cons}:
            return found, ("That contract is not one this broker's files on "
                           f"{prog.name if prog else 'this programme'} are written under.")
        cid = contract_id
    elif len(cons) <= 1:
        cid = cons[0].id if cons else None        # none: held by the contract check
    else:
        named = _named_in(text, cons, lambda c: (contract_ref(c), c.name, contract_label(c),
                                                 *_code_forms(contract_code(c))))
        if len(named) != 1:
            return found, (f"Which contract? {prog.name if prog else 'This programme'} has "
                           f"{len(cons)} contracts for this broker: "
                           f"{_listed(f'{contract_label(c)} ({contract_code(c)})' for c in cons)}. "
                           f"Name one, or its code, in {where}.")
        cid = named[0].id
    found["contract_id"] = cid

    # ── period ──
    from submission_calendar import parse_period_hint
    label, named_date = (period or "").strip() or None, None
    source = "explicit" if label else None
    if not label:
        from submission_calendar_service import resolve_period_label
        named_date = parse_period_hint(period_hint) or parse_period_hint(filename)
        label, source = resolve_period_label(session, pid,
                                             covering_date=parse_period_hint(period_hint),
                                             source_filename=filename)
        if not label and named_date is not None and not _has_calendar(session, pid):
            label = f"{named_date:%Y-%m}"         # said, and no calendar to hold it to
            source = "date" if parse_period_hint(period_hint) else "filename"
    allowed = reporting_periods(session, tenant, pid, broker)
    if not label:
        if named_date is None:
            return found, (f"No reporting period. Say which period the file is for in "
                           f"{where}, for example Bordereau_2026-09.xlsx or "
                           f"\"September 2026\".")
        if _month_not_over(f"{named_date:%Y-%m}"):
            return found, _not_ended_reason(f"{named_date:%Y-%m}", allowed)
        return found, (f"{named_date:%B %Y} is not a reporting period of this "
                       f"programme. Name one of its periods in {where}.")
    if allowed and label not in allowed:
        from db import ExpectedSubmission
        later = (session.query(ExpectedSubmission.id)
                 .filter(ExpectedSubmission.program_id == pid,
                         ExpectedSubmission.period == label).first())
        # A month still running is "not yet", never "not a period": the
        # calendar only holds months that have started, so the current one
        # (or a later one) can be missing from it and still be perfectly real.
        if later is not None or _month_not_over(label):
            return found, _not_ended_reason(label, allowed)
        return found, (f"{label} is not a reporting period of this programme. "
                       f"Name one of its periods in {where}, e.g. {allowed[0]}.")
    found["period"], found["period_source"] = label, source
    return found, None


def _period_name(label: str) -> str:
    """"2026-10" -> "October 2026"; any other label (a quarter, say) as it is."""
    try:
        return datetime.strptime(label, "%Y-%m").strftime("%B %Y")
    except ValueError:
        return label


def _month_not_over(label: str) -> bool:
    """True for a "YYYY-MM" month that has not ended yet (this month or later)."""
    try:
        month = datetime.strptime(label, "%Y-%m")
    except ValueError:
        return False
    now = datetime.utcnow()
    return (month.year, month.month) >= (now.year, now.month)


def _not_ended_reason(label: str, allowed: list[str]) -> str:
    """Why a file for a period still running is refused, in plain words."""
    unit = "month" if re.fullmatch(r"\d{4}-\d{2}", label) else "period"
    latest = (f" — the latest open period is {_period_name(allowed[0])}"
              if allowed else "")
    return (f"{_period_name(label)} has not ended yet. A bordereau can be sent once "
            f"the {unit} is over{latest}.")


def _has_calendar(session, program_id: int) -> bool:
    from db import SubmissionSchedule
    return (session.query(SubmissionSchedule.id)
            .filter(SubmissionSchedule.program_id == program_id).first() is not None)


class _RouteFor:
    """The route as the later checks see it: narrowed to the programme the file
    was identified for. A route pinned to a programme reads exactly as itself;
    a broker-wide one is checked against the programme its file named."""

    def __init__(self, route: IntakeRoute, program_id: Optional[int]):
        self._route = route
        self.program_id = program_id if program_id is not None else route.program_id

    def __getattr__(self, name):
        return getattr(self._route, name)


def _check_not_duplicate(session, tenant_id: int, sha: str,
                         route: Optional[IntakeRoute],
                         scope: Optional[dict] = None) -> Optional[str]:
    """A file we have loaded before, arriving again.

    Only a PREVIOUSLY ACCEPTED file counts. Matching against refused arrivals
    too would mean a broker who fixes nothing and resends gets "duplicate"
    instead of the real reason their file was refused.

    FOR THE SAME THING. Given `scope` (programme, contract, period — what the
    file was identified as), only an earlier file for that same programme,
    contract and period counts: the same spreadsheet sent for another
    programme or another month is a different bordereau, and holding it as a
    copy of the first filed it under the wrong programme's submission.
    """
    q = (session.query(FileArrival)
         .filter(FileArrival.tenant_id == tenant_id,
                 FileArrival.file_hash_sha256 == sha,
                 FileArrival.outcome == "accepted"))
    scope = scope or {}
    if scope.get("program_id") is not None and scope.get("period"):
        q = (q.outerjoin(IntakeRoute, IntakeRoute.id == FileArrival.route_id)
              .filter(func.coalesce(FileArrival.program_id, IntakeRoute.program_id)
                      == scope["program_id"],
                      FileArrival.reporting_period == scope["period"]))
        if scope.get("contract_id") is not None:
            q = q.filter(or_(FileArrival.contract_id == scope["contract_id"],
                             FileArrival.contract_id.is_(None)))
    prior = q.order_by(FileArrival.id.desc()).first()
    if prior is None:
        return None
    # The file is still held whoever sent the first copy — two brokers
    # delivering identical bytes is as likely to double-count premium as one
    # broker sending twice. But the WHEN belongs to whoever sent it: this
    # message goes back to the sender, and another broker's load date is not
    # theirs to learn.
    sender = route.broker_party_id if route is not None else None
    if sender is not None and prior.matched_broker_party_id == sender:
        when = (prior.received_at.strftime("%d %b %Y")
                if prior.received_at else "earlier")
        return f"Held — exactly the same file we already loaded on {when}."
    return "Held — exactly the same file has already been loaded."


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
    from db import Contract, Program, ProgramBroker
    # Eligibility, not currency: the question is "does this broker hold an
    # APPROVED contract on the programme", not "which version is newest". Since
    # currency stopped being stored, `status == 'active'` would no longer narrow
    # anything here — and narrowing by date would be wrong, because a file may
    # legitimately arrive for a period whose contract has since expired.
    #
    # WHOSE contract: this broker's own, or one the carrier holds for the whole
    # programme (broker NULL — the same rule the programme's contract list
    # uses). Another broker's contract on a shared programme says nothing about
    # this broker, and neither does a contract at a different carrier: the
    # programme must be this route's carrier's.
    from contract_upload_services.contract_asof import NON_GOVERNING_STATUSES
    q = (session.query(Contract.id)
         .join(ProgramBroker, ProgramBroker.program_id == Contract.program_id)
         .join(Program, Program.id == Contract.program_id)
         .filter(ProgramBroker.broker_party_id == route.broker_party_id,
                 ProgramBroker.status == "active",
                 Program.tenant_id == route.tenant_id,
                 or_(Contract.broker_party_id == route.broker_party_id,
                     Contract.broker_party_id.is_(None)),
                 func.coalesce(Contract.status, "").notin_(NON_GOVERNING_STATUSES)))
    # When the route names its programme (10.2), check THAT programme's
    # contract rather than "any contract this broker holds anywhere" — which is
    # all the old mesh note could manage without a program_id column.
    program_id = getattr(route, "program_id", None)
    if program_id is not None:
        q = q.filter(Contract.program_id == program_id)
    live = q.first()
    if live is None:
        return ("Held — there is no active contract for this broker yet. There is "
                "nothing to check a file against until the contract is agreed.")
    return None


# ── the stored copy, when there is no blob storage ──────────────────────────
#
# With STORAGE_BACKEND=db, storage.store_or_keep hands the bytes BACK instead of
# a blob ref, and file_arrival has no bytes column — so a file that came in by
# API, SFTP or email was checked, recorded and then had nothing left to run.
# The copy goes in its own table (migration 31) and the arrival's blob_ref is
# set to DB_COPY_REF, so every existing "is there a copy?" test keeps working.
#
# A WAITING ROOM, not a second store: the copy only bridges arrival → run (auto
# run, a held file released days later, Run again after a failure). Once a run
# succeeds, mark_run deletes it — the rows are in landing_record by then, which
# is all a broker's own Process Bordereau run leaves behind too. Manual uploads
# never use it: they are run there and then from the bytes in hand.
#
# Best-effort on purpose: before migration 31 is run the table is missing, and
# a file must still be recorded exactly as it was before.

DB_COPY_REF = "db:file_arrival_file"


def keep_copy(session, arrival: FileArrival, file_bytes: bytes) -> None:
    """Keep the file's bytes in the database when blob storage kept none."""
    if arrival.blob_ref or not file_bytes:
        return
    from sqlalchemy import text
    try:
        with session.begin_nested():
            session.execute(text(
                "INSERT INTO file_arrival_file (arrival_id, tenant_id, file_bytes) "
                "VALUES (:a, :t, :b) ON CONFLICT (arrival_id) DO NOTHING"),
                {"a": arrival.id, "t": arrival.tenant_id, "b": file_bytes})
        arrival.blob_ref = DB_COPY_REF
    except Exception as exc:   # noqa: BLE001 — no traceback: it would print the file
        log.warning("could not keep a copy of arrival %s (is migration 31 run?): %s",
                    arrival.public_ref, str(exc).splitlines()[0][:200])


def read_copy(blob_ref: Optional[str], arrival_id: int) -> Optional[bytes]:
    """The stored bytes of an arrival, wherever they were kept, or None."""
    if not blob_ref:
        return None
    if blob_ref != DB_COPY_REF:
        import storage
        return storage.resolve_bytes(blob_ref, None)
    from sqlalchemy import text
    from db import SessionLocal
    with SessionLocal() as s:
        row = s.execute(text("SELECT file_bytes FROM file_arrival_file "
                             "WHERE arrival_id = :a"), {"a": arrival_id}).first()
    return bytes(row[0]) if row else None


def drop_copy(session, arrival_id: int) -> None:
    """Delete an arrival's database copy (retention). The row stays."""
    from sqlalchemy import text
    with session.begin_nested():
        session.execute(text("DELETE FROM file_arrival_file WHERE arrival_id = :a"),
                        {"a": arrival_id})


# ── the landing pipeline ────────────────────────────────────────────────────

def land_file(session, *, tenant_id: int, filename: str, file_bytes: bytes,
              route: Optional[IntakeRoute] = None,
              claimed_sender: Optional[str] = None,
              idempotency_key: Optional[str] = None,
              public_ref: Optional[str] = None,
              blob_ref: Optional[str] = None,
              max_bytes: Optional[int] = None,
              period: Optional[str] = None,
              period_hint: Optional[str] = None,
              replaces: Optional[str] = None,
              program_id: Optional[int] = None,
              contract_id: Optional[int] = None,
              refusal: Optional[str] = None,
              declared_size: Optional[int] = None) -> FileArrival:
    """Record one arriving file and decide whether it may go on.

    `refusal` is a reason the channel has already found (an email that did not
    copy the carrier): it is checked right after the sender, like any check.

    `declared_size` is a size the channel knows WITHOUT having read the file (a
    remote SFTP listing — sftp_pull). Over the cap, the file is refused on it
    with its real size and `file_bytes` may be empty: it was never downloaded.

    `period`, `program_id` and `contract_id` are what the sender STATED (the
    API's fields, the secure link's corrected data). Whatever is not stated is
    read from `period_hint` (an email subject) and the file name, or taken as
    read when there is only one answer — see identify().

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

    NOT DONE HERE: running an accepted file. It is left with run_state NULL and
    intake_autorun runs it on its own thread, so a slow run never holds up the
    email or SFTP collector that is calling this.
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

    # What the file is for — programme, contract, period — as Process Bordereau
    # asks a person. Filled by the identification check; the later checks read
    # the programme from it.
    ident: dict = {}

    def _identified() -> Optional[str]:
        found, why = identify(session, route, filename=filename, period=period,
                              period_hint=period_hint, program_id=program_id,
                              contract_id=contract_id)
        ident.update(found)
        return why

    def _scoped():
        return _RouteFor(route, ident.get("program_id")) if route is not None else None

    reason: Optional[str] = None
    for check in (
        # ── settled without opening the file ────────────────────────────────
        lambda: safety.check_size(file_bytes, max_bytes, size=declared_size),
        lambda: _check_is_spreadsheet(filename, file_bytes),
        lambda: safety.check_safe_to_open(filename, file_bytes),
        lambda: _check_known_sender(route),
        lambda: refusal,
        _identified,
        # Scanned before anything else is decided about it — the same order as
        # a manual upload, and the order Files Received shows.
        lambda: safety.scan_for_malware(filename, file_bytes),
        lambda: _check_not_duplicate(session, tenant_id, sha, route, scope=ident),
        # ── from here on the file gets opened ───────────────────────────────
        lambda: _check_can_open(rows()),
        lambda: _check_has_rows(rows()),
        lambda: required_fields.check(session, _scoped(), filename, file_bytes),
        lambda: _check_live_contract(session, _scoped()),
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
        file_size_bytes=(len(file_bytes) if declared_size is None
                         else max(int(declared_size), len(file_bytes))),
        row_count=row_count,
        file_hash_sha256=sha,
        received_at=datetime.now(timezone.utc),
        outcome=outcome,
        turned_away_reason=reason,
        public_ref=public_ref or new_public_ref(),
        idempotency_key=idempotency_key,
        blob_ref=blob_ref,
        # What it is for, as far as it was identified — a file refused for
        # "which contract?" still shows its programme on Files Received.
        program_id=ident.get("program_id"),
        contract_id=ident.get("contract_id"),
        reporting_period=ident.get("period"),
    )
    session.add(arrival)
    session.flush()
    keep_copy(session, arrival, file_bytes)

    # THE CALENDAR'S "turned up" MOMENT (requirement 17.2). An accepted file on a
    # route pinned to a programme satisfies that programme's period for this
    # broker — which is the whole point of the calendar knowing when something
    # was due. Only ACCEPTED files count: a turned-away file did not arrive, and
    # a held one has not been decided yet, so neither may tick a deadline off.
    #
    # Which period: one the sender STATED (the API's `period`, already checked
    # against the calendar) is trusted outright. Otherwise a hint the sender
    # wrote (an email subject such as "July 2026 bordereau"), then the file
    # NAME — so July's bordereau arriving in September still lands on July.
    # A file that names no period is refused by identify() above, so it never
    # gets here; mark_received's oldest-open fallback is left for programmes
    # with no calendar to name.
    #
    # Best-effort on purpose: the calendar is a side-feature, and nothing here
    # may stop a file being recorded as arrived.
    cal_program = ident.get("program_id") or (route.program_id if route is not None else None)
    stated = ident.get("period") or period or None
    if outcome == "accepted" and route is not None and cal_program is not None:
        try:
            from submission_calendar import parse_period_hint
            from submission_calendar_service import mark_received
            received = mark_received(
                session, cal_program,
                received_on=arrival.received_at.date(),
                broker_party_id=route.broker_party_id,
                source_filename=filename, period=stated,
                covering_date=None if stated else parse_period_hint(period_hint),
                period_source=ident.get("period_source"))
            # Kept on the arrival (a column since migration 32) so the API
            # receipt can say which period the file was recorded against, and a
            # correction for the same period finds this file's submission.
            arrival.reporting_period = (received["period"] if received
                                        else arrival.reporting_period or stated)
            session.flush()
        except Exception:   # noqa: BLE001
            log.warning("submission-calendar mark_received failed for arrival %s",
                        arrival.public_ref, exc_info=True)

    # The broker exception loop: this file is a new submission, or the next
    # version of one (by the reference it quotes, or by programme + contract +
    # period — whichever way the earlier file came in).
    # Bookkeeping only — it never changes what happens to the file — and it
    # cannot fail the landing (on_land swallows its own errors).
    import submission_service
    submission_service.on_land(session, arrival, route,
                               period=arrival.reporting_period,
                               replaces=replaces, hint=period_hint)

    return arrival


class DuplicateUpload(Exception):
    """A hand-uploaded file we have already accepted. Not a refusal: the person
    is right there, so they are ASKED — "run it anyway?" — instead of the file
    being held for somebody to find later."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def land_manual_upload(session, *, tenant_id: int, filename: str, file_bytes: bytes,
                       user_id: Optional[int], broker_party_id: Optional[int],
                       program_id: Optional[int], confirm_duplicate: bool = False,
                       blob_ref: Optional[str] = None,
                       max_bytes: Optional[int] = None,
                       period: Optional[str] = None,
                       replaces: Optional[str] = None,
                       contract_id: Optional[int] = None) -> FileArrival:
    """Record a file uploaded by hand on Process Bordereau as an arrival.

    The same door as email, SFTP and API, with the checks that make sense for a
    logged-in person who has already picked the programme and contract:

      size · spreadsheet · safe to open · security scan · can open · has rows
          refused exactly as land_file refuses them (turned_away, reason kept)
      duplicate
          raised as DuplicateUpload unless `confirm_duplicate` — the person
          decides on the spot. A confirmed duplicate is recorded as released,
          with the reason, so "who loaded this twice?" stays answerable.
      sender, live contract
          not asked: the sender is the login, and the contract was chosen on
          the screen and is checked again by the run itself.

    The caller commits. `run_state` starts as 'running' so the auto-run worker
    never takes a file somebody is already running by hand.
    """
    sha = hashlib.sha256(file_bytes).hexdigest()
    _counted: list = []

    def rows() -> Optional[int]:
        if not _counted:
            _counted.append(safety.with_timeout(
                lambda: count_rows(filename, file_bytes)))
        return _counted[0]

    reason: Optional[str] = None
    for check in (
        lambda: safety.check_size(file_bytes, max_bytes),
        lambda: _check_is_spreadsheet(filename, file_bytes),
        lambda: safety.check_safe_to_open(filename, file_bytes),
        lambda: safety.scan_for_malware(filename, file_bytes),
        lambda: _check_can_open(rows()),
        lambda: _check_has_rows(rows()),
    ):
        reason = check()
        if reason:
            break

    # "Held" has no meaning for a person who is still on the page: an empty
    # file is simply refused, with the same words, and they fix it and retry.
    if reason and reason.startswith("Held — "):
        reason = reason[len("Held — "):]
        reason = reason[:1].upper() + reason[1:]
    # The contract the person picked — or, when they did not have to pick, the
    # only one there is. Kept on the file, as for every other way in.
    if contract_id is None and program_id is not None:
        only = live_contracts(session, tenant_id, program_id, broker_party_id)
        if len(only) == 1:
            contract_id = only[0].id
    # A copy only of a file for the same programme, contract and month.
    duplicate = None if reason else _check_not_duplicate(
        session, tenant_id, sha, None,
        scope={"program_id": program_id, "contract_id": contract_id, "period": period})
    if duplicate and not confirm_duplicate:
        msg = duplicate.replace("Held — ", "", 1)
        raise DuplicateUpload(msg[:1].upper() + msg[1:])

    arrival = FileArrival(
        tenant_id=tenant_id,
        route_id=None,
        channel="upload",
        program_id=program_id,
        contract_id=contract_id,
        reporting_period=period or None,
        submitted_by_user_id=user_id,
        matched_broker_party_id=broker_party_id,
        claimed_sender=f"user:{user_id}" if user_id else None,
        filename=filename,
        file_size_bytes=len(file_bytes),
        row_count=_counted[0] if _counted else None,
        file_hash_sha256=sha,
        received_at=datetime.now(timezone.utc),
        outcome="turned_away" if reason else "accepted",
        # A confirmed duplicate keeps the reason it would have been held for,
        # alongside the decision — the same record a released held file has.
        turned_away_reason=reason or duplicate,
        public_ref=new_public_ref(),
        blob_ref=blob_ref,
        run_state=None if reason else "running",
    )
    if duplicate and not reason:
        arrival.resolution = "released"
        arrival.resolved_at = arrival.received_at
        arrival.resolved_by_user_id = user_id
        arrival.resolution_note = "Run anyway at upload"
    session.add(arrival)
    session.flush()
    if arrival.outcome == "accepted":
        # The broker exception loop (bookkeeping only; never raises).
        import submission_service
        submission_service.on_land(session, arrival, None, period=period,
                                   program_id=program_id, replaces=replaces)
    return arrival


def mark_run(arrival_id: int, *, state: str, landing_id: Optional[int] = None,
             export_id: Optional[int] = None, error: Optional[str] = None) -> None:
    """Record how an arrival's run went. Its own session and commit, because it
    is called from inside a run whose own sessions have long closed — and a
    failure to record must never turn a finished run into an error."""
    from db import SessionLocal
    try:
        with SessionLocal() as s:
            a = s.get(FileArrival, arrival_id)
            if a is None:
                return
            a.run_state = state
            a.run_at = datetime.now(timezone.utc)
            if landing_id is not None:
                a.run_landing_id = landing_id
            if export_id is not None:
                a.run_export_id = export_id
            # Cleared on success: an old error must not sit beside a clean run.
            a.run_error = (error or "")[:2000] or None if state != "done" else None
            # Processed: the database copy has done its job (see DB_COPY_REF).
            # A failed run keeps it, so Run again still has a file to run.
            if state == "done" and a.blob_ref == DB_COPY_REF:
                try:
                    drop_copy(s, a.id)
                    a.blob_ref = None
                except Exception:  # noqa: BLE001 — never lose the "done" itself
                    log.warning("could not delete the copy of arrival %s",
                                arrival_id, exc_info=True)
            s.commit()
    except Exception:  # noqa: BLE001
        log.warning("could not record the run of arrival %s", arrival_id, exc_info=True)
        return
    # Tell the broker how it went (and deliver it if the programme's rule is
    # met). Its own session; never raises.
    if state in ("done", "failed", "not_run"):
        import submission_service
        submission_service.on_run_outcome(arrival_id)


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
