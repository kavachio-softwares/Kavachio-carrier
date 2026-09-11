"""Feature 10.1 — the collector that picks files out of the SFTP folders.

This is deliberately the smallest part of the feature. It takes anything in an
enabled SFTP route's `incoming` folder that has finished uploading, hands it to
intake_service.land_file, and moves it out of the way so it is never read twice.

WHEN IT LOOKS. It used to look in every folder on a five-minute timer, so a file
sat unseen for up to five minutes. Now the operating system says when something
lands — sftp_watch (FSEvents on macOS, inotify on Linux) — and the collector
runs within about a second. Two things still run on a clock, and neither is the
old poll:

  * a RECHECK, when a file was still being written: it is looked at again as
    soon as SFTP_QUIET_SECONDS has passed, rather than at the next sweep;
  * a slow BACKUP SWEEP (SFTP_SWEEP_SECONDS), because the OS can drop events
    under a burst and a network mount delivers none at all.

If the watcher cannot run — watchdog not installed, or SFTP_WATCH_ENABLED=0 for
a network mount — the collector falls back to the old timer, SFTP_POLL_SECONDS.

It talks to the FILESYSTEM, not to SSH. Whatever serves SFTP in front of that
directory — a self-hosted sshd chrooted there, or an Azure Blob SFTP mount —
this file does not change. That also means the whole feature is testable today
by copying a file into a folder, with no SSH server anywhere.

ON BY DEFAULT. Collecting is what the screen promises a broker, so the app has
to do it without anybody remembering to set a variable. Set
SFTP_POLLER_ENABLED=0 to stop it; POST /intake/routes/{id}/poll still works
either way.

Configuration:
  SFTP_POLLER_ENABLED   0/1     (default 1 — on)
  SFTP_WATCH_ENABLED    0/1     (default 1) set 0 where SFTP_ROOT is a network
                                mount (NFS, SMB, blobfuse): those deliver no events
  SFTP_SWEEP_SECONDS    int     (default 900)  backup sweep while watching
  SFTP_POLL_SECONDS     int     (default 300)  the timer, only when not watching
  SFTP_ROOT             path    (default ./sftp-root)         see intake_service
  SFTP_QUIET_SECONDS    int     (default 10)                  see intake_service
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

import intake_safety as svc_safety
import intake_service as svc
import sftp_watch
import storage
from db import SessionLocal
from intake_models import IntakeRoute

log = logging.getLogger("kavachio.sftp_poller")

_thread: threading.Thread | None = None
_stop = threading.Event()
# Set by the folder watcher's thread; read by the collector's.
_wake = threading.Event()
# What the collector is actually doing, for the screen: watching | timer | off.
_mode = "off"

# How long a burst of events is left to settle before looking. Copying twelve
# files into a folder is twelve events; one look collects all twelve.
_SETTLE_SECONDS = 1.0

# After a pass that failed outright (the database was unreachable, say), how
# soon to try again. Waiting for the backup sweep would leave a file the watcher
# already announced sitting there for up to SFTP_SWEEP_SECONDS.
_RETRY_SECONDS = 30

# Namespace for the Postgres advisory locks below, so a route lock cannot
# collide with an advisory lock taken by some other part of the system.
_LOCK_NAMESPACE = 0x5F7B  # "sftp"


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _enabled() -> bool:
    return _flag("SFTP_POLLER_ENABLED", "1")


def _watch_enabled() -> bool:
    return _flag("SFTP_WATCH_ENABLED", "1")


def _seconds(name: str, default: int, floor: int = 10) -> int:
    try:
        return max(floor, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _interval() -> int:
    """The timer, used only when the folders cannot be watched."""
    return _seconds("SFTP_POLL_SECONDS", 300)


def _sweep_interval() -> int:
    """The backup sweep while watching. Slow on purpose — it only catches what
    the watcher missed."""
    return _seconds("SFTP_SWEEP_SECONDS", 900, floor=60)


def status() -> dict:
    """How files are being noticed in this process, for the Ways in panel."""
    mode = _mode if _enabled() else "off"
    return {"mode": mode,
            "check_seconds": _sweep_interval() if mode == "watching" else _interval()}


def _stamped(name: str) -> str:
    """Prefix a filename with the time it was handled.

    Two files with the same name land in `processed` every month — a broker who
    always sends "bordereau.xlsx" would otherwise overwrite their own history
    the second time. The stamp sorts chronologically too, which is what anyone
    opening the folder actually wants.
    """
    return f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{name}"


def collect_route(session, route: IntakeRoute) -> dict:
    """Collect one route's folder once.

    Returns a small summary so a manual poll can say what happened rather than
    just spinning.

    ORDER MATTERS, and it is the one thing in this file worth being careful
    about: read the bytes, record the arrival, COMMIT, and only then move the
    file. Moving first and crashing would take the file out of `incoming` with
    no row anywhere to say it ever existed — a broker's whole month gone, with
    nothing to find it by. Doing it in this order can at worst re-read a file
    after a crash, and the duplicate check catches that.
    """
    # Creates any folder this route is missing, so a route made before `held`
    # and `quarantine` existed grows them on its next poll (12.3).
    svc.ensure_route_dirs(route)
    incoming = svc.route_dir(route, "incoming")
    summary = {"route_id": route.id, "address": route.address,
               "looked_in": str(incoming), "accepted": 0, "held": 0,
               "turned_away": 0, "skipped_still_writing": 0, "files": []}

    if not incoming.is_dir():
        summary["error"] = "folder does not exist yet"
        return summary

    # One worker per route. Several app replicas each run their own collector,
    # and unlike the daily calendar sweep this one is NOT harmless when it
    # doubles up: two workers reading the same file would land it twice and
    # double the premium. Whoever gets the lock does the work; the others move
    # on. With a watcher on every replica they all wake at once, so this lock
    # now matters on every single arrival, not just on an unlucky timer tick.
    #
    # The lock is taken on a DEDICATED connection, inside its own transaction,
    # for two reasons that are easy to get wrong:
    #
    #  * dedicated connection — an advisory lock belongs to the connection that
    #    took it, and the `session.commit()` inside the loop below hands the
    #    session's connection back to the pool. A lock taken on the session
    #    would be unlocked on whatever connection came next, so the real lock
    #    would leak and every later poll would report "already collecting".
    #  * xact_lock, not the session-level one — a transaction-scoped lock is
    #    released when the transaction ends, including by the ROLLBACK the pool
    #    issues when a connection is returned. A session-level lock survives
    #    that, so a crash between lock and unlock would wedge this route until
    #    the process restarted.
    lock_key = (_LOCK_NAMESPACE << 32) | (route.id & 0xFFFFFFFF)
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
        summary["skipped"] = "another worker is already collecting this folder"
        return summary

    try:
        for path in sorted(p for p in incoming.iterdir() if p.is_file()):
            # Dotfiles (.DS_Store, editor swap files) and uploads still wearing
            # a temporary name (.filepart, .part). The second used to be taken
            # once it had been quiet long enough — half a bordereau, followed by
            # the whole one as a "duplicate" the moment the client renamed it.
            if sftp_watch.is_temp_name(path.name):
                continue
            # Finished if nothing has touched it for the quiet window — or if
            # the watcher saw it renamed from a temporary name, which IS the
            # client saying "done" and needs no waiting out.
            if not (svc.is_quiet(path) or sftp_watch.finished_by_rename(path)):
                summary["skipped_still_writing"] += 1
                continue

            try:
                file_bytes = path.read_bytes()
            except OSError as exc:
                log.warning("could not read %s: %s", path, exc)
                continue

            # 12.3 — our own copy, before any decision is made.
            #
            # The file is also MOVED into rejected/held/quarantine below, and
            # for an operator browsing the folder that is the copy they want.
            # This one is for the review screen: it is what the download button
            # reads, and what retention can expire on a schedule. Without it
            # SFTP arrivals were the only ones a reviewer could not open.
            blob_ref = None
            try:
                blob_ref, _ = storage.store_or_keep(
                    "intake", route.tenant_id, path.name, file_bytes)
            except Exception as exc:
                log.error("could not store %s: %s", path.name, exc)

            arrival = svc.land_file(
                session, tenant_id=route.tenant_id, filename=path.name,
                file_bytes=file_bytes, route=route, blob_ref=blob_ref,
                # On SFTP the folder is the claim — there is no From: header to
                # take a sender's word for.
                claimed_sender=f"sftp:{route.address}",
            )
            session.commit()

            # Where a file goes says what it IS, and the four destinations are
            # four different situations (12.3):
            #
            #   quarantine  failed the security scan. `rejected` is where
            #               somebody goes to look at what was refused, and
            #               handing them an infected workbook to open is the one
            #               outcome worse than not catching it.
            #   processed   accepted and loaded.
            #   held        waiting on a person — a suspected duplicate, a
            #               changed layout, no contract yet. NOT a refusal, and
            #               burying these in `rejected` meant the only pile
            #               anybody has to work sat inside the pile nobody does.
            #   rejected    genuinely bad; the broker has to fix and resend.
            if svc_safety.is_malware_reason(arrival.turned_away_reason):
                destination = svc.route_dir(route, "quarantine")
            elif arrival.outcome == "accepted":
                destination = svc.route_dir(route, "processed")
            elif arrival.outcome == "held":
                destination = svc.route_dir(route, "held")
            else:
                destination = svc.route_dir(route, "rejected")
            destination.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(path), str(destination / _stamped(path.name)))
                sftp_watch.forget(path)
            except OSError as exc:
                # The row is committed, so the file is accounted for. Leaving it
                # in `incoming` is safe — the duplicate check refuses it next
                # time rather than loading it twice.
                log.error("landed %s but could not move it: %s", path.name, exc)

            summary.setdefault(arrival.outcome, 0)
            summary[arrival.outcome] += 1
            summary["files"].append({
                "filename": path.name,
                "outcome": arrival.outcome,
                "reason": arrival.turned_away_reason,
                "arrival_id": arrival.id,
            })
    finally:
        # Ending the transaction is what releases the lock; nothing else has to
        # happen for it to be given up.
        lock_txn.rollback()
        lock_conn.close()

    return summary


def collect_all() -> dict:
    """Every enabled SFTP route, across every tenant. Safe to run repeatedly."""
    totals = {"routes": 0, "accepted": 0, "held": 0, "turned_away": 0,
              "skipped_still_writing": 0}
    with SessionLocal() as s:
        routes = (s.query(IntakeRoute)
                  .filter(IntakeRoute.channel == "sftp",
                          IntakeRoute.is_enabled.is_(True))
                  .order_by(IntakeRoute.id).all())
        for route in routes:
            try:
                result = collect_route(s, route)
                totals["routes"] += 1
                for key in ("accepted", "held", "turned_away", "skipped_still_writing"):
                    totals[key] += result.get(key, 0)
            except Exception as exc:
                # One broken folder must not stop the others being collected.
                s.rollback()
                log.exception("collecting route %s failed: %s", route.id, exc)
        s.commit()
    return totals


def _run() -> None:
    """The collector's thread: sleep until something lands, then collect.

    A thread rather than an asyncio task because everything it does blocks —
    the filesystem, the database, the virus scanner — and the watcher calls
    back from a thread of its own anyway.
    """
    global _mode
    watcher = sftp_watch.FolderWatcher(svc.sftp_root(), _wake.set)
    watching = _watch_enabled() and watcher.start()
    _mode = "watching" if watching else "timer"
    if watching:
        log.info("sftp collector: collecting the moment a file lands in %s "
                 "(backup sweep every %ss)", svc.sftp_root(), _sweep_interval())
    else:
        log.info("sftp collector: not watching — checking %s every %ss",
                 svc.sftp_root(), _interval())

    # Due at once: whatever landed while the app was down is collected on start.
    next_sweep = 0.0
    recheck_at: float | None = None
    try:
        while not _stop.is_set():
            due = next_sweep if recheck_at is None else min(next_sweep, recheck_at)
            if _wake.wait(max(0.0, due - time.monotonic())):
                if _stop.wait(_SETTLE_SECONDS):
                    break
                # Cleared AFTER the settle, so everything that landed during it
                # is covered by this pass; anything landing during the pass sets
                # it again and gets a pass of its own.
                _wake.clear()
            if _stop.is_set():
                break

            failed = False
            totals: dict = {}
            try:
                totals = collect_all()
                if totals["accepted"] or totals["held"] or totals["turned_away"]:
                    log.info("sftp: %s", totals)
            except Exception as exc:
                # Never let a bad pass kill the collector; retry shortly.
                failed = True
                log.exception("sftp collection failed: %s", exc)

            # A watcher that has died is restarted; one that cannot be is
            # replaced by the timer rather than leaving the folders unwatched.
            if watching and not watcher.alive:
                log.warning("sftp watcher stopped — restarting it")
                watcher.stop()
                watching = watcher.start()
                _mode = "watching" if watching else "timer"

            now = time.monotonic()
            next_sweep = now + (_sweep_interval() if watching else _interval())
            if failed:
                recheck_at = now + _RETRY_SECONDS
            elif totals.get("skipped_still_writing"):
                # Look again the moment the quiet window can have passed, not
                # at the next sweep. A file still growing is skipped again and
                # re-armed again, so a long upload is followed to its end.
                recheck_at = now + svc.quiet_seconds() + 1
            else:
                recheck_at = None
    finally:
        watcher.stop()
        _mode = "off"


def start(app) -> None:
    """Attach the collector to the app's startup, the same way sweep_scheduler does."""
    if not _enabled():
        log.info("sftp collector off (SFTP_POLLER_ENABLED=0) — nothing will be "
                 "collected from the folders")
        return

    @app.on_event("startup")
    async def _start_sftp_collector() -> None:    # pragma: no cover - wiring
        global _thread
        if _thread is None or not _thread.is_alive():
            _stop.clear()
            _thread = threading.Thread(target=_run, name="sftp-collector", daemon=True)
            _thread.start()

    @app.on_event("shutdown")
    async def _stop_sftp_collector() -> None:     # pragma: no cover - wiring
        # Daemon thread: it stops within a second, and never holds up exit if
        # it is halfway through a large file.
        _stop.set()
        _wake.set()
