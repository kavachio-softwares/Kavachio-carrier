"""Feature 10.1 — the collector that picks files out of the SFTP folders.

This is deliberately the smallest part of the feature. It looks in each enabled
SFTP route's `incoming` folder every few minutes, takes anything that has
finished uploading, hands it to intake_service.land_file, and moves it out of
the way so it is never read twice.

It talks to the FILESYSTEM, not to SSH. Whatever serves SFTP in front of that
directory — a self-hosted sshd chrooted there, or an Azure Blob SFTP mount —
this file does not change. That also means the whole feature is testable today
by copying a file into a folder, with no SSH server anywhere.

OFF BY DEFAULT. Set SFTP_POLLER_ENABLED=1 to run it. Nothing in the app behaves
differently until you do.

Configuration:
  SFTP_POLLER_ENABLED   0/1     (default 0 — off)
  SFTP_POLL_SECONDS     int     (default 300 — the design's "every 5 minutes")
  SFTP_ROOT             path    (default ./sftp-root)         see intake_service
  SFTP_QUIET_SECONDS    int     (default 30)                  see intake_service
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

import intake_safety as svc_safety
import intake_service as svc
import storage
from db import SessionLocal
from intake_models import IntakeRoute

log = logging.getLogger("kavachio.sftp_poller")

_task: asyncio.Task | None = None

# Namespace for the Postgres advisory locks below, so a route lock cannot
# collide with an advisory lock taken by some other part of the system.
_LOCK_NAMESPACE = 0x5F7B  # "sftp"


def _enabled() -> bool:
    return os.getenv("SFTP_POLLER_ENABLED", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _interval() -> int:
    try:
        return max(10, int(os.getenv("SFTP_POLL_SECONDS", "300")))
    except ValueError:
        return 300


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

    Returns a small summary so the manual "poll now" button on the screen can
    say what happened rather than just spinning.

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

    # One worker per route. Several app replicas each run their own poller, and
    # unlike the daily calendar sweep this one is NOT harmless when it doubles
    # up: two workers reading the same file would land it twice and double the
    # premium. Whoever gets the lock does the work; the others move on.
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
            if path.name.startswith("."):
                continue                      # editor swap files, .DS_Store
            if not svc.is_quiet(path):
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
    totals = {"routes": 0, "accepted": 0, "held": 0, "turned_away": 0}
    with SessionLocal() as s:
        routes = (s.query(IntakeRoute)
                  .filter(IntakeRoute.channel == "sftp",
                          IntakeRoute.is_enabled.is_(True))
                  .order_by(IntakeRoute.id).all())
        for route in routes:
            try:
                result = collect_route(s, route)
                totals["routes"] += 1
                totals["accepted"] += result.get("accepted", 0)
                totals["held"] += result.get("held", 0)
                totals["turned_away"] += result.get("turned_away", 0)
            except Exception as exc:
                # One broken folder must not stop the others being collected.
                s.rollback()
                log.exception("collecting route %s failed: %s", route.id, exc)
        s.commit()
    return totals


async def _loop() -> None:
    from fastapi.concurrency import run_in_threadpool
    interval = _interval()
    log.info("sftp poller started — every %ss, root=%s", interval, svc.sftp_root())
    while True:
        try:
            totals = await run_in_threadpool(collect_all)
            if totals["accepted"] or totals["turned_away"]:
                log.info("sftp poll: %s", totals)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never let a bad poll kill the loop; the next one retries.
            log.exception("sftp poll failed: %s", exc)
        await asyncio.sleep(interval)


def start(app) -> None:
    """Attach the poller to the app's startup, the same way sweep_scheduler does."""
    if not _enabled():
        log.info("sftp poller disabled (set SFTP_POLLER_ENABLED=1 to run it)")
        return

    @app.on_event("startup")
    async def _start_sftp_poller() -> None:       # pragma: no cover - wiring
        global _task
        if _task is None or _task.done():
            _task = asyncio.create_task(_loop())

    @app.on_event("shutdown")
    async def _stop_sftp_poller() -> None:        # pragma: no cover - wiring
        global _task
        if _task is not None:
            _task.cancel()
            _task = None
