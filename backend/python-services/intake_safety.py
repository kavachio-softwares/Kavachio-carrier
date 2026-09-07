"""Feature 12.1 — the checks that must run BEFORE anything opens the file.

`land_file` used to count the rows in an arriving file as the very first thing
it did. That meant openpyxl, pandas and the XML parser were handed a stranger's
bytes before anything had judged them — the one step in the whole pipeline that
executes attacker-chosen input, running before every check meant to protect it.

This module is what goes in that gap. Everything here answers a question that
can be settled WITHOUT parsing the file:

  * Is it small enough to be worth reading at all?
  * Is it a zip that claims to be 4 GB once opened? (an .xlsx IS a zip)
  * Does it carry macros we have not agreed to accept?
  * Does the virus scanner recognise it?

Only when all four say nothing does `count_rows` get to run — and even then
inside a timeout, because a clean file can still be pathological.

Nothing here needs a new package. ClamAV is spoken to over its own wire protocol
with `socket`, and the XML hardening uses `defusedxml` when it happens to be
installed and falls back to blocking entity declarations at the expat level when
it is not. Both are one small function; neither is worth a dependency.
"""
from __future__ import annotations

import io
import logging
import os
import re
import socket
import struct
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _Timeout
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

log = logging.getLogger("kavachio.intake.safety")

# A refused file whose reason starts with this is not a broker mistake — it is
# an incident. The SFTP collector reads it to decide that the file goes into
# `quarantine/` rather than `rejected/`, where somebody might open it. A shared
# constant rather than a string match in two files, so the wording can change
# without quietly re-routing infected files back into the review folder.
MALWARE_PREFIX = "Refused for safety —"


def is_malware_reason(reason: Optional[str]) -> bool:
    return bool(reason) and reason.startswith(MALWARE_PREFIX)


# ── configuration ───────────────────────────────────────────────────────────
# Read at call time for the same reason intake_service does it: this module is
# imported before main.py runs load_dotenv().

def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def max_bytes() -> int:
    """The largest file we will even look at. 200 MB matches the figure the API
    already promises partners; SFTP had no cap at all before this."""
    return _int_env("INTAKE_MAX_FILE_MB", 200) * 1024 * 1024


def _max_uncompressed() -> int:
    """How big an .xlsx may claim to be once unzipped. A real bordereau of
    500,000 rows unzips to well under this; a zip bomb does not."""
    return _int_env("INTAKE_MAX_UNZIPPED_MB", 2048) * 1024 * 1024


def _max_ratio() -> int:
    """Compressed-to-uncompressed ratio that stops looking like a spreadsheet
    and starts looking like a weapon. Spreadsheets compress well — 20x is
    ordinary, 50x happens — so the trigger is deliberately far above that."""
    return _int_env("INTAKE_MAX_ZIP_RATIO", 200)


def _max_entries() -> int:
    return _int_env("INTAKE_MAX_ZIP_ENTRIES", 5000)


def _allow_macros() -> bool:
    """Default ON, and deliberately so: .xlsm has always been accepted, and
    turning that off by deploying this file would refuse files that worked
    yesterday. Set INTAKE_ALLOW_MACROS=0 when you are ready to say no."""
    return _flag("INTAKE_ALLOW_MACROS", "1")


def parse_timeout() -> int:
    return _int_env("INTAKE_PARSE_TIMEOUT_SECONDS", 60)


def av_backend() -> str:
    """`none` (default) or `clamav`. Off by default because a scanner that is
    configured but unreachable holds every file — that has to be a deliberate
    switch somebody threw, never something a deploy turned on by surprise."""
    return os.getenv("INTAKE_AV_BACKEND", "none").strip().lower()


def _av_fail_open() -> bool:
    """What to do when the scanner is enabled but does not answer. Default is
    fail CLOSED: the file is held, not accepted. An unscanned file is not a
    clean file, and a held file can be released in one click once the scanner is
    back — an accepted infected one cannot be un-accepted."""
    return os.getenv("INTAKE_AV_ON_ERROR", "hold").strip().lower() == "accept"


# ── 12.1a — size ────────────────────────────────────────────────────────────

def check_size(file_bytes: bytes, cap: Optional[int] = None) -> Optional[str]:
    """First question, and the only one that costs nothing at all.

    Before this existed the API capped uploads at 200 MB, email capped
    attachments at 25 MB by silently dropping them, and SFTP capped nothing —
    so the one door with no human watching it was the one that would hand a
    3 GB file to openpyxl.
    """
    cap = cap or max_bytes()
    if len(file_bytes) > cap:
        return (f"The file is {len(file_bytes) // (1024 * 1024)} MB. "
                f"We can accept files up to {cap // (1024 * 1024)} MB. "
                f"Splitting the month into separate files usually fixes this.")
    if not file_bytes:
        return "The file is empty — nothing arrived at all."
    return None


# ── 12.1b — is it safe to open? ─────────────────────────────────────────────

def _zip_findings(file_bytes: bytes) -> Optional[str]:
    """What the zip's own index says it contains, without extracting anything.

    An .xlsx is a zip, and a zip carries a directory listing the uncompressed
    size of every entry. Reading that costs nothing and is enough to refuse the
    two shapes that hurt: a handful of entries that expand to gigabytes, and a
    listing with a hundred thousand entries in it.

    The listing can lie — a crafted zip can under-report — but the absolute cap
    below is checked against the claim AND the claim is what openpyxl trusts
    when it allocates, so a lie large enough to matter is a lie we catch.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            infos = zf.infolist()
            if len(infos) > _max_entries():
                return (f"The workbook contains {len(infos)} internal parts, "
                        f"far more than a spreadsheet has. We cannot open it.")
            total = sum(max(0, i.file_size) for i in infos)
            if total > _max_uncompressed():
                return (f"The file is small but expands to "
                        f"{total // (1024 * 1024)} MB when opened. "
                        f"We cannot open it.")
            ratio = total / max(1, len(file_bytes))
            if ratio > _max_ratio() and total > 64 * 1024 * 1024:
                return ("The file expands to hundreds of times its own size "
                        "when opened, which no spreadsheet does. "
                        "We cannot open it.")
            if not _allow_macros():
                for i in infos:
                    if i.filename.lower().endswith("vbaproject.bin"):
                        return ("The workbook contains macros. Please send the "
                                "same data saved as .xlsx, without macros.")
    except zipfile.BadZipFile:
        # Not a zip. `_check_is_spreadsheet` has already refused anything named
        # .xlsx that is not one, so reaching here means a format that is not
        # zip-based and has nothing for this check to say.
        return None
    except Exception as exc:                       # pragma: no cover - defensive
        log.warning("zip inspection failed: %s", exc)
        return None
    return None


def check_safe_to_open(filename: str, file_bytes: bytes) -> Optional[str]:
    """Everything that must be settled before a parser touches the bytes."""
    ext = Path(filename).suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        return _zip_findings(file_bytes)
    return None


# ── 12.1c — hardened XML ────────────────────────────────────────────────────

# A DOCTYPE is where both XML attacks live: entity declarations that expand to
# gigabytes ("billion laughs") and external references that ask the parser to go
# and read a file off the server. A bordereau is a list of policies. It has no
# legitimate reason to declare an entity or a doctype, ever — so the cheapest
# correct answer is to refuse the construct outright rather than to parse it
# carefully. Checked over the first few KB, since a declaration has to come
# before the root element.
_DOCTYPE_HEAD = 8192
_DANGEROUS_XML = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


def safe_xml_root(file_bytes: bytes):
    """Parse XML without the two tricks that turn a parser into a liability.

    Two layers, because neither alone is enough in every deployment:

      1. `defusedxml` when it is installed — the library written for exactly
         this, and what requirements.txt now pins.
      2. A refusal of any DOCTYPE or ENTITY declaration in the file's head.
         This runs FIRST and needs no package at all, which matters because a
         host that has not picked up the new requirement would otherwise fall
         through to the stdlib parser, which does both attacks by default.

    The handler-level hardening that would once have been the fallback is not
    an option: CPython no longer exposes the underlying expat parser on
    `ElementTree.XMLParser`, so there is nothing left to attach a handler to.
    """
    if _DANGEROUS_XML.search(file_bytes[:_DOCTYPE_HEAD]):
        raise ValueError("XML doctype and entity declarations are not accepted")
    try:
        from defusedxml.ElementTree import fromstring as _safe_fromstring
        return _safe_fromstring(file_bytes)
    except ImportError:
        return ET.fromstring(file_bytes)


# ── 12.1d — a parse that cannot run forever ─────────────────────────────────

_T = TypeVar("_T")


def with_timeout(fn: Callable[[], _T], seconds: Optional[int] = None,
                 on_timeout: _T = None) -> _T:
    """Run `fn`, and give up waiting after `seconds`.

    Honest about what this is: the worker thread is not killed, because Python
    cannot kill one. What it guarantees is that the CALLER — an API request, a
    poller loop — is never stuck behind a pathological file. The size and
    expansion caps above are what actually bound the abandoned thread, which is
    why they run first and this runs last.
    """
    seconds = seconds or parse_timeout()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fn)
        try:
            return future.result(timeout=seconds)
        except _Timeout:
            log.warning("giving up on a file that took over %ss to read", seconds)
            pool.shutdown(wait=False, cancel_futures=True)
            return on_timeout
        except Exception as exc:
            log.info("parse failed: %s", exc)
            return on_timeout


# ── 12.1e — virus scanning ──────────────────────────────────────────────────

_CHUNK = 1 << 16          # clamd's own INSTREAM chunk size


def _clamav_verdict(file_bytes: bytes) -> tuple[str, str]:
    """('clean'|'found'|'error', detail) straight from clamd.

    Spoken over clamd's INSTREAM protocol: `zINSTREAM\\0`, then each chunk as a
    4-byte big-endian length followed by the bytes, then a zero length to say
    that is all. The reply is one line — `stream: OK` or
    `stream: Eicar-Test-Signature FOUND`.

    Written against a socket rather than the `clamd` package on purpose: it is
    thirty lines, it has no dependency to keep patched, and the protocol has not
    changed in fifteen years.
    """
    unix_socket = os.getenv("INTAKE_CLAMAV_SOCKET", "").strip()
    host = os.getenv("INTAKE_CLAMAV_HOST", "127.0.0.1").strip()
    port = _int_env("INTAKE_CLAMAV_PORT", 3310)
    timeout = _int_env("INTAKE_AV_TIMEOUT_SECONDS", 30)

    sock = None
    try:
        if unix_socket:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(unix_socket)
        else:
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.settimeout(timeout)

        sock.sendall(b"zINSTREAM\0")
        for start in range(0, len(file_bytes), _CHUNK):
            chunk = file_bytes[start:start + _CHUNK]
            sock.sendall(struct.pack("!I", len(chunk)) + chunk)
        sock.sendall(struct.pack("!I", 0))

        reply = b""
        while b"\0" not in reply and len(reply) < 4096:
            part = sock.recv(4096)
            if not part:
                break
            reply += part
    except Exception as exc:
        return "error", str(exc)
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    text = reply.replace(b"\0", b"").decode("utf-8", "replace").strip()
    if text.endswith("OK"):
        return "clean", text
    if "FOUND" in text:
        # "stream: Eicar-Test-Signature FOUND" -> the signature name.
        name = text.split(":", 1)[-1].replace("FOUND", "").strip()
        return "found", name or "malware"
    return "error", text or "no reply from the scanner"


def scan_for_malware(filename: str, file_bytes: bytes) -> Optional[str]:
    """A refusal reason when the file is not safe to keep, else None.

    Three outcomes, and the middle one is the one that matters:

      clean  -> None, carry on.
      found  -> a refusal prefixed with MALWARE_PREFIX. The broker is told the
                file could not be accepted and nothing more; naming the
                signature tells whoever sent it exactly what to change to get
                past us next time.
      error  -> "Held —" by default. An unscanned file is not a clean file, and
                a held file can be released in one click when the scanner comes
                back. An accepted infected one cannot be taken back.
    """
    if av_backend() != "clamav":
        return None

    verdict, detail = _clamav_verdict(file_bytes)

    if verdict == "clean":
        return None
    if verdict == "found":
        log.error("malware in %s: %s", filename, detail)
        return (f"{MALWARE_PREFIX} this file did not pass our security scan "
                f"and has not been kept. Please check the machine it was "
                f"exported from before sending it again.")

    log.error("virus scanner unreachable while checking %s: %s", filename, detail)
    if _av_fail_open():
        return None
    return ("Held — the security scan could not run, so the file has not been "
            "opened. It will be picked up as soon as scanning is available.")
