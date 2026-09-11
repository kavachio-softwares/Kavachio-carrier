"""Feature 10 — telling an open Files screen the moment a file lands.

Files Received used to ask for the whole list every 60 seconds, just in case.
Now the server says when something changed and the screen asks once.

In two halves:

  SENDING. Every flush that writes a `file_arrival` row also runs
  pg_notify('intake_arrivals', '<tenant_id>') in the same transaction. Postgres
  delivers a NOTIFY only if that transaction COMMITS, and folds identical ones
  within a transaction into one — so a screen is never told about a row that
  was rolled back, and never told twice for one commit. It is hooked on the
  session factory, so every way in — SFTP, email, API, a reviewer's decision —
  is covered without any of them having to remember.

  RECEIVING. One thread per process LISTENs and hands each notification to the
  browsers connected to that process (GET /intake/events → stream() below).

Postgres rather than an in-memory signal because the process that lands a file
is often not the one holding a given browser's connection: every replica runs
its own collectors. Only "tenant N changed" crosses the wire — never a filename
or a row — so a screen still reads what it shows through its normal,
tenant-scoped endpoint.

Needs a direct Postgres connection for the listener: LISTEN does not survive a
pgbouncer in transaction-pooling mode.

Configuration:
  INTAKE_LIVE_ENABLED  0/1  (default 1)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import select
import threading
import time
from typing import AsyncIterator

log = logging.getLogger("kavachio.intake_events")

CHANNEL = "intake_arrivals"

# A feed is closed after this long and the browser reconnects at once. The
# bearer token is only checked when a feed opens, so this bounds how long a
# signed-out or expired session goes on hearing "something changed".
STREAM_MAX_SECONDS = 15 * 60
# Written when nothing else has been: stops proxies closing a quiet response,
# and is how a browser that has gone away is noticed.
PING_SECONDS = 20
# A listener connection this quiet gets a round trip, which both keeps it open
# through firewalls that drop idle sockets and notices one that already died.
_LISTENER_KEEPALIVE_SECONDS = 60

_hook_installed = False
_listener: threading.Thread | None = None
_stop = threading.Event()
_listening = False
# Raised by the exit signal or the app's shutdown; every feed checks it each
# second. A plain bool on purpose — it is set from inside a signal handler,
# where taking a lock could deadlock.
_closing = False

_subs_lock = threading.Lock()
_subs: set["_Subscriber"] = set()


def enabled() -> bool:
    return os.getenv("INTAKE_LIVE_ENABLED", "1").strip().lower() in (
        "1", "true", "yes", "on",
    )


# ── sending ─────────────────────────────────────────────────────────────────

def _notify_after_flush(session, flush_context) -> None:
    """Queue a NOTIFY for every tenant whose arrivals this flush wrote.

    `session.new` and `session.dirty` still hold the pre-flush state here,
    which is exactly the set of rows this flush wrote.
    """
    from sqlalchemy import text
    from intake_models import FileArrival

    tenants = {obj.tenant_id for obj in (*session.new, *session.dirty)
               if isinstance(obj, FileArrival) and obj.tenant_id is not None}
    if not tenants:
        return
    conn = session.connection()
    if conn.dialect.name != "postgresql":
        return
    for tenant_id in sorted(tenants):
        conn.execute(text("SELECT pg_notify(:c, :p)"),
                     {"c": CHANNEL, "p": str(tenant_id)})


def install() -> None:
    """Hook the NOTIFY onto every session. Safe to call more than once."""
    global _hook_installed
    if _hook_installed:
        return
    from sqlalchemy import event
    from db import SessionLocal
    event.listen(SessionLocal, "after_flush", _notify_after_flush)
    _hook_installed = True


# ── receiving ───────────────────────────────────────────────────────────────

class _Subscriber:
    """One open feed: the tenant it is for, and how to wake it."""

    def __init__(self, tenant_id: int, loop: asyncio.AbstractEventLoop):
        self.tenant_id = tenant_id
        self.loop = loop
        # One slot. While a signal is waiting to be read, more add nothing —
        # the screen re-reads everything anyway.
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=1)


def _offer(queue: asyncio.Queue) -> None:
    if not queue.full():
        queue.put_nowait(True)


def publish(tenant_id: int | None) -> None:
    """Wake every feed in this process for `tenant_id` (None: all of them).

    Safe from any thread — the listener calls it from its own.
    """
    with _subs_lock:
        subs = list(_subs)
    for sub in subs:
        if tenant_id is None or sub.tenant_id == tenant_id:
            try:
                sub.loop.call_soon_threadsafe(_offer, sub.queue)
            except RuntimeError:
                pass                                # its event loop has closed


def _line(payload: dict) -> bytes:
    return (json.dumps(payload) + "\n").encode()


async def stream(tenant_id: int, request) -> AsyncIterator[bytes]:
    """The feed for one browser: `ready`, then `arrivals` whenever this
    tenant's files change, with a `ping` when it has been quiet."""
    sub = _Subscriber(tenant_id, asyncio.get_running_loop())
    with _subs_lock:
        _subs.add(sub)
    started = last_line = time.monotonic()
    try:
        yield _line({"type": "ready", "live": _listening})
        while not _closing and time.monotonic() - started < STREAM_MAX_SECONDS:
            try:
                # One-second slices so a shutdown is noticed promptly.
                await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if time.monotonic() - last_line >= PING_SECONDS:
                    if await request.is_disconnected():
                        break
                    yield _line({"type": "ping"})
                    last_line = time.monotonic()
                continue
            yield _line({"type": "arrivals"})
            last_line = time.monotonic()
    finally:
        with _subs_lock:
            _subs.discard(sub)


def _dsn() -> str:
    from db import engine
    # psycopg2 wants plain postgresql://, not SQLAlchemy's +psycopg2 dialect.
    return engine.url.set(drivername="postgresql").render_as_string(hide_password=False)


def _listen() -> None:
    """The listener thread: LISTEN, fan notifications out, reconnect on loss."""
    global _listening
    import psycopg2

    backoff = 1
    heard_before = False
    while not _stop.is_set():
        conn = None
        try:
            conn = psycopg2.connect(_dsn())
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f"LISTEN {CHANNEL}")
            _listening = True
            backoff = 1
            log.info("intake events: listening on '%s'", CHANNEL)
            if heard_before:
                # Anything committed while we were deaf went unannounced, so
                # every open screen checks once.
                publish(None)
            heard_before = True

            quiet_since = time.monotonic()
            while not _stop.is_set():
                if select.select([conn], [], [], 1.0)[0]:
                    conn.poll()
                    while conn.notifies:
                        note = conn.notifies.pop(0)
                        try:
                            publish(int(note.payload))
                        except ValueError:
                            pass
                    quiet_since = time.monotonic()
                elif time.monotonic() - quiet_since >= _LISTENER_KEEPALIVE_SECONDS:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                    quiet_since = time.monotonic()
        except Exception as exc:
            _listening = False
            log.warning("intake events: lost the database listener (%s) — "
                        "retrying in %ss", exc, backoff)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
            _stop.wait(backoff)
            backoff = min(backoff * 2, 60)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    _listening = False


def _end_feeds_on_exit_signal() -> None:
    """Close every feed as soon as the process is told to stop.

    uvicorn waits for every open response to finish BEFORE it runs the app's
    shutdown hooks, and a feed never finishes by itself — so a --reload or a
    Ctrl+C would sit on "Waiting for connections to close" for up to
    STREAM_MAX_SECONDS. The exit signal is the only earlier moment, so it is
    chained: raise the flag the feeds check each second, then hand the signal to
    whoever had it before (uvicorn) exactly as it would have gone anyway.
    """
    import signal
    if threading.current_thread() is not threading.main_thread():
        return                            # handlers can only be set from there
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous = signal.getsignal(signum)
        if getattr(previous, "_ends_intake_feeds", False):
            continue

        def handler(sig, frame, _previous=previous):
            global _closing
            _closing = True
            if callable(_previous):
                _previous(sig, frame)
            elif _previous != signal.SIG_IGN:
                signal.signal(sig, signal.SIG_DFL)
                os.kill(os.getpid(), sig)

        handler._ends_intake_feeds = True
        signal.signal(signum, handler)


def start(app) -> None:
    """Hook the NOTIFY and run the listener with the app."""
    if not enabled():
        log.info("intake events off (INTAKE_LIVE_ENABLED=0) — the Files screen "
                 "will only change when somebody presses Refresh")
        return
    install()

    @app.on_event("startup")
    async def _start_intake_listener() -> None:   # pragma: no cover - wiring
        global _listener, _closing
        from db import engine
        if engine.dialect.name != "postgresql":
            log.warning("intake events need Postgres LISTEN/NOTIFY — not started")
            return
        _closing = False
        _end_feeds_on_exit_signal()
        if _listener is None or not _listener.is_alive():
            _stop.clear()
            _listener = threading.Thread(target=_listen, name="intake-events",
                                         daemon=True)
            _listener.start()

    @app.on_event("shutdown")
    async def _stop_intake_listener() -> None:    # pragma: no cover - wiring
        global _closing
        _closing = True
        _stop.set()
