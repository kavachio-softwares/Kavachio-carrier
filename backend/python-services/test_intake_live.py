"""Tests for instant intake: the IMAP IDLE conversation, the SFTP folder
watcher, and the feed that tells an open Files screen something landed.

No database and no mail server — fake sockets, a temp folder, a local event loop.

Run:  python -m pytest test_intake_live.py
"""
import asyncio
import imaplib
import json
import threading
import time
from types import SimpleNamespace

import pytest

import imap_idle
import intake_events
import sftp_watch


# ── IMAP IDLE ───────────────────────────────────────────────────────────────

class FakeSock:
    """Plays a mail server. `during` is what it sends while idling, `after`
    what it sends once the client says DONE. None in a script is a read that
    times out; b"" is the server hanging up."""

    def __init__(self, during, after=()):
        self.during, self.after = list(during), list(after)
        self.sent: list[bytes] = []
        self.timeout = None

    def settimeout(self, t):
        self.timeout = t

    def recv(self, _n):
        script = self.after if b"DONE\r\n" in self.sent else self.during
        if not script:
            time.sleep(min(self.timeout or 0, 0.02))
            raise TimeoutError
        item = script.pop(0)
        if item is None:
            raise TimeoutError
        return item

    def sendall(self, data):
        self.sent.append(data)


class FakeImap:
    def __init__(self, sock):
        self.sock = sock

    def send(self, data):
        self.sock.sendall(data)


def _idle(during, after=(), seconds=0.3, stop=None):
    sock = FakeSock(during, after)
    got = imap_idle.wait_for_mail(FakeImap(sock), imap_idle.LineReader(sock),
                                  seconds, stop)
    return got, sock


def test_new_mail_ends_the_wait_and_idle_is_closed_properly():
    got, sock = _idle([b"+ idling\r\n", b"* 3 EXISTS\r\n"],
                      [b"* 3 RECENT\r\n", b"KVIDLE OK Idle completed\r\n"],
                      seconds=30)
    assert got is True
    assert sock.sent == [b"KVIDLE IDLE\r\n", b"DONE\r\n"]


def test_a_quiet_mailbox_waits_out_the_refresh_and_reports_nothing():
    started = time.monotonic()
    got, sock = _idle([b"+ idling\r\n"], [b"KVIDLE OK\r\n"], seconds=0.3)
    assert got is False
    assert time.monotonic() - started >= 0.3
    assert sock.sent[-1] == b"DONE\r\n"


def test_the_collectors_own_moves_and_flags_do_not_wake_it():
    got, _ = _idle([b"+ idling\r\n", b"* 2 EXPUNGE\r\n",
                    b"* 1 FETCH (FLAGS (\\Seen))\r\n"], [b"KVIDLE OK\r\n"])
    assert got is False


def test_lines_split_across_packets_are_reassembled():
    got, _ = _idle([b"+ idl", b"ing\r\n* 4 EX", None, b"ISTS\r\n"],
                   [b"KVIDLE O", b"K done\r\n"], seconds=30)
    assert got is True


def test_mail_announced_while_saying_done_still_counts():
    got, _ = _idle([b"+ idling\r\n"], [b"* 9 EXISTS\r\n", b"KVIDLE OK\r\n"])
    assert got is True


def test_server_hanging_up_raises_so_the_caller_reconnects():
    with pytest.raises(ConnectionError):
        _idle([b"+ idling\r\n", b"* BYE shutting down\r\n"])
    with pytest.raises(ConnectionError):
        _idle([b"+ idling\r\n", b""])


def test_idle_refused_raises():
    with pytest.raises(imaplib.IMAP4.error):
        _idle([b"KVIDLE BAD unknown command\r\n"])


def test_stop_ends_a_long_wait_promptly():
    stop = threading.Event()
    stop.set()
    started = time.monotonic()
    got, sock = _idle([b"+ idling\r\n"], [b"KVIDLE OK\r\n"], seconds=60, stop=stop)
    assert got is False and time.monotonic() - started < 2
    assert sock.sent[-1] == b"DONE\r\n"


def test_idle_support_is_read_from_capability():
    has = SimpleNamespace(capability=lambda: ("OK", [b"IMAP4rev1 LITERAL+ IDLE"]))
    lacks = SimpleNamespace(capability=lambda: ("OK", [b"IMAP4rev1 LITERAL+"]))
    assert imap_idle.server_supports_idle(has) is True
    assert imap_idle.server_supports_idle(lacks) is False


# ── SFTP folder watcher ─────────────────────────────────────────────────────

def test_temporary_upload_names():
    for name in (".bdx.xlsx", "bdx.xlsx.filepart", "bdx.PART", "bdx.xlsx.tmp"):
        assert sftp_watch.is_temp_name(name), name
    assert not sftp_watch.is_temp_name("bordereau-2026-09.xlsx")


def test_rename_from_a_temporary_name_marks_the_upload_finished(tmp_path):
    incoming = tmp_path / "broker" / "incoming"
    incoming.mkdir(parents=True)
    final = incoming / "bdx.xlsx"
    final.write_bytes(b"PK\x03\x04 a whole workbook")
    woke = []
    handler = sftp_watch._Wake(lambda: woke.append(1))

    handler.on_moved(SimpleNamespace(is_directory=False,
                                     src_path=str(incoming / "bdx.xlsx.filepart"),
                                     dest_path=str(final)))
    assert woke and sftp_watch.finished_by_rename(final)

    # Written to again after the rename: somebody is still at it, so the quiet
    # window applies after all.
    final.write_bytes(b"PK\x03\x04 a whole workbook, and then some more")
    assert not sftp_watch.finished_by_rename(final)
    sftp_watch.forget(final)


def test_only_incoming_wakes_the_collector(tmp_path):
    woke = []
    handler = sftp_watch._Wake(lambda: woke.append(1))
    base = tmp_path / "broker"
    # The collector's own move into processed/, and an upload still under a
    # temporary name — neither is a reason to look.
    handler.on_moved(SimpleNamespace(is_directory=False,
                                     src_path=str(base / "incoming" / "a.xlsx"),
                                     dest_path=str(base / "processed" / "a.xlsx")))
    handler.on_created(SimpleNamespace(is_directory=False,
                                       src_path=str(base / "incoming" / "a.xlsx.filepart")))
    assert woke == []
    handler.on_created(SimpleNamespace(is_directory=False,
                                       src_path=str(base / "incoming" / "a.xlsx")))
    assert woke == [1]


@pytest.mark.skipif(sftp_watch.Observer is None, reason="watchdog not installed")
def test_the_real_watcher_notices_a_file_landing(tmp_path):
    incoming = tmp_path / "carrier" / "broker" / "incoming"
    incoming.mkdir(parents=True)
    landed = threading.Event()
    watcher = sftp_watch.FolderWatcher(tmp_path, landed.set)
    assert watcher.start()
    try:
        time.sleep(0.5)                  # FSEvents starts reporting asynchronously
        (incoming / "bordereau.xlsx").write_bytes(b"PK\x03\x04")
        assert landed.wait(5), "no event within 5 seconds"
    finally:
        watcher.stop()


# ── the Files screen feed ───────────────────────────────────────────────────

async def _connected():
    return False


def test_feed_announces_changes_for_its_own_tenant_only():
    async def run():
        feed = intake_events.stream(7, SimpleNamespace(is_disconnected=_connected))
        assert json.loads(await feed.__anext__())["type"] == "ready"

        nxt = asyncio.ensure_future(feed.__anext__())
        # Published from another thread, the way the listener does it.
        threading.Thread(target=intake_events.publish, args=(8,)).start()
        await asyncio.sleep(0.3)
        assert not nxt.done(), "another tenant's arrival reached this feed"

        threading.Thread(target=intake_events.publish, args=(7,)).start()
        assert json.loads(await asyncio.wait_for(nxt, 3))["type"] == "arrivals"

        await feed.aclose()
        assert not intake_events._subs
    asyncio.run(run())


def test_feed_closes_promptly_on_shutdown():
    async def run():
        feed = intake_events.stream(7, SimpleNamespace(is_disconnected=_connected))
        await feed.__anext__()
        intake_events._closing = True
        try:
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(feed.__anext__(), 3)
        finally:
            intake_events._closing = False
    asyncio.run(run())
