"""Feature 10.3 — noticing new mail the moment it arrives, with IMAP IDLE.

email_poller used to log in every five minutes and ask "anything new?". IDLE
(RFC 2177) turns that round: one connection stays logged in and tells the
server "I am waiting", and the server answers "* 12 EXISTS" the instant a
message lands. The collector then reads the mailbox exactly as before.

Like sftp_watch, this only ever WAKES the collector. Parsing, matching a broker,
landing and filing the message away all stay in email_poller.collect_mailbox, on
a connection of its own — so mail noticed here and mail found by the backup
sweep go through the same code, and this connection never does anything but
wait.

WHY IT READS THE SOCKET ITSELF. Python 3.14 adds imaplib.IMAP4.idle(); the
Dockerfile runs 3.12. imaplib reads through a buffered file object, and a
timeout on one of those poisons it for good ("cannot read from timed out
object"). IDLE is nothing BUT waiting with a deadline, so the conversation is
read straight off the socket, where a timeout is harmless.

Pure protocol: no database, no configuration — testable with a fake socket.
"""
from __future__ import annotations

import imaplib
import re
import threading
import time
from typing import Optional

# Our own tag rather than imaplib's counter, so imaplib never waits for a
# response to a command it did not send.
TAG = b"KVIDLE"

# "* 12 EXISTS" — the folder grew. RECENT comes with it on most servers. EXPUNGE
# and FETCH (flag changes) are deliberately NOT here: the collector's own work on
# the other connection produces those, and waking for them would loop.
_NEW_MAIL = re.compile(rb"^\*\s+\d+\s+(EXISTS|RECENT)\b", re.I)

# How long the server gets to acknowledge IDLE or DONE. A healthy server answers
# in milliseconds; silence this long means the connection is dead.
_ANSWER_SECONDS = 30.0


class LineReader:
    """CRLF-terminated lines straight off a socket, each with a deadline."""

    def __init__(self, sock):
        self._sock = sock
        self._buf = b""

    def readline(self, timeout: float) -> Optional[bytes]:
        """The next line without its CRLF, or None if none arrives in time.

        A partial line is kept for the next call, so a line split across
        packets — or across a timeout — is never lost or mangled.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while b"\r\n" not in self._buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._sock.settimeout(remaining)
            try:
                chunk = self._sock.recv(65536)
            except TimeoutError:            # socket.timeout is TimeoutError
                return None
            if not chunk:
                raise ConnectionError("the mail server closed the connection")
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\r\n")
        return line


def server_supports_idle(imap: imaplib.IMAP4) -> bool:
    """Asked AFTER login — some servers only list IDLE to a signed-in user."""
    try:
        typ, data = imap.capability()
    except Exception:
        return False
    if typ != "OK" or not data:
        return False
    words = b" ".join(d for d in data if isinstance(d, bytes)).upper().split()
    return b"IDLE" in words


def _check_bye(line: bytes) -> None:
    if line[:5].upper() == b"* BYE":
        raise ConnectionError(
            f"the mail server hung up: {line.decode('ascii', 'replace')}")


def wait_for_mail(imap, reader: LineReader, seconds: float,
                  stop: Optional[threading.Event] = None) -> bool:
    """Wait in IDLE for up to `seconds`. True when the folder gained mail.

    Ends IDLE either way before returning, so the connection is back in the
    ordinary selected state. Ending it on a schedule is not optional: servers
    drop an IDLE left running for 30 minutes (RFC 2177), and NATs and load
    balancers drop a silent connection far sooner. Raises when the connection
    is dead — the caller reconnects.
    """
    new_mail = False

    imap.send(TAG + b" IDLE\r\n")
    while True:
        line = reader.readline(_ANSWER_SECONDS)
        if line is None:
            raise TimeoutError("the mail server did not answer IDLE")
        _check_bye(line)
        if line.startswith(b"+"):
            break                                   # "+ idling" — it is listening
        if line.startswith(TAG + b" "):
            raise imaplib.IMAP4.error(
                f"IDLE refused: {line.decode('ascii', 'replace')}")
        if _NEW_MAIL.match(line):
            new_mail = True

    deadline = time.monotonic() + max(0.0, seconds)
    while not new_mail and not (stop is not None and stop.is_set()):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # Short reads, so a shutdown is noticed within a second.
        line = reader.readline(min(remaining, 1.0))
        if line is None:
            continue
        _check_bye(line)
        if _NEW_MAIL.match(line):
            new_mail = True

    imap.send(b"DONE\r\n")
    while True:
        line = reader.readline(_ANSWER_SECONDS)
        if line is None:
            raise TimeoutError("the mail server did not finish IDLE")
        _check_bye(line)
        if _NEW_MAIL.match(line):
            new_mail = True                         # landed while we were saying DONE
        if line.startswith(TAG + b" "):
            if not line[len(TAG) + 1:].upper().startswith(b"OK"):
                raise imaplib.IMAP4.error(
                    f"IDLE ended badly: {line.decode('ascii', 'replace')}")
            return new_mail
