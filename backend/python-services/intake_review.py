"""Feature 12.3 — working the quarantine queue.

The screen could always SHOW a refused file. Nothing could ever resolve one:
`file_arrival` recorded what arrived and what the checks made of it, and had no
column anywhere for what a PERSON decided. So the release and discard buttons
were built disabled — honestly disabled, with a comment saying the endpoint did
not exist — and a held file stayed held for ever.

This module is that missing half. Four things:

  release   "this is fine, load it" — for a file the checks could not settle,
            never for one they refused outright.
  discard   "ignore this" — the file stays on the record, marked dealt with.
  fetch     the bytes, so somebody can actually open what they are judging.
  purge     forget the bytes after a while, keep the record for ever.

The rule underneath all four: a decision is a fact about the file, so it is
recorded, never a deletion. Three months on, "why is March short?" has to be
answerable, and "somebody discarded the file on the 6th and here is their note"
is an answer. A row that quietly vanished is not.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import audit
import intake_safety as safety
import storage
from intake_models import FileArrival

log = logging.getLogger("kavachio.intake.review")


class ReviewError(Exception):
    """Something a person needs told, not a bug. The route turns it into a 400."""


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def retention_days() -> int:
    """How long a refused or held file's BYTES are kept.

    Not for ever, and not because of disk. These are broker files full of
    policyholder data; keeping them indefinitely is a liability rather than
    thoroughness. Ninety days covers the "we sent it in March" argument and the
    monthly cycle that produced it.
    """
    return _int_env("INTAKE_RETENTION_DAYS", 90)


def quarantine_retention_days() -> int:
    """Infected files go sooner. Long enough to answer "what was it and who
    sent it", short enough that we are not warehousing malware."""
    return _int_env("INTAKE_QUARANTINE_RETENTION_DAYS", 7)


def is_infected(arrival: FileArrival) -> bool:
    return safety.is_malware_reason(arrival.turned_away_reason)


def _stamp(arrival: FileArrival, resolution: str, user_id: Optional[int],
           note: Optional[str]) -> None:
    arrival.resolution = resolution
    arrival.resolved_at = datetime.now(timezone.utc)
    arrival.resolved_by_user_id = user_id
    arrival.resolution_note = (note or "").strip() or None


# ── the two decisions ───────────────────────────────────────────────────────

def release(session, arrival: FileArrival, *, user_id: Optional[int],
            note: Optional[str] = None) -> FileArrival:
    """"This is fine, load it."

    HELD ONLY, and that is the whole distinction 12.3 rests on. A held file is
    good — a suspected duplicate, a layout that moved, a contract signed a day
    late — and a person overruling the check is exactly what "held" means. A
    turned-away file is one we could not read: releasing a PDF or a truncated
    workbook would hand the pipeline something it cannot process, and the honest
    fix is for the broker to send a file we can open.

    An infected file is never releasable, whatever anybody clicks.
    """
    if is_infected(arrival):
        raise ReviewError("This file failed the security scan. It cannot be "
                          "released, and it has not been kept.")
    # Resolution first. Releasing flips the outcome to accepted, so asking
    # about the outcome first would answer a second click with "only a held file
    # can be released" — true, and completely the wrong thing to say.
    if arrival.resolution:
        raise ReviewError(f"Somebody already {arrival.resolution} this file.")
    if arrival.outcome != "held":
        raise ReviewError(
            "Only a held file can be released. This one was turned away "
            f"because: {arrival.turned_away_reason or 'no reason recorded'}")

    # The person has overruled the check, so the file IS accepted now. The
    # original reason stays on the row — "released despite X" is the useful
    # record, and blanking it would lose why anybody had to decide at all.
    arrival.outcome = "accepted"
    _stamp(arrival, "released", user_id, note)
    session.flush()

    audit.log_activity(arrival.tenant_id, str(user_id or "?"),
                       "intake.arrival.released", target=arrival.public_ref,
                       details={"filename": arrival.filename,
                                "held_because": arrival.turned_away_reason,
                                "note": arrival.resolution_note})
    # NOT DONE HERE, and the same seam land_file leaves alone: handing the file
    # to the processing pipeline. `bdx_upload_id` stays NULL. A released file is
    # accepted and waiting, exactly like every other accepted file.
    return arrival


def discard(session, arrival: FileArrival, *, user_id: Optional[int],
            note: Optional[str] = None) -> FileArrival:
    """"Ignore this."

    Works on anything unresolved, including an infected file — somebody still
    has to be able to close that off. Deliberately NOT a delete: the row stays,
    the reason stays, and who decided is now on it. Retention removes the bytes
    later; nothing ever removes the fact that a file arrived.
    """
    if arrival.resolution:
        raise ReviewError(f"Somebody already {arrival.resolution} this file.")
    if arrival.outcome == "accepted":
        raise ReviewError("This file was accepted. There is nothing to discard.")

    _stamp(arrival, "discarded", user_id, note)
    session.flush()
    audit.log_activity(arrival.tenant_id, str(user_id or "?"),
                       "intake.arrival.discarded", target=arrival.public_ref,
                       details={"filename": arrival.filename,
                                "reason": arrival.turned_away_reason,
                                "note": arrival.resolution_note})
    return arrival


# ── looking at the file ─────────────────────────────────────────────────────

def fetch_bytes(arrival: FileArrival, *, user_id: Optional[int],
                ip: Optional[str] = None) -> bytes:
    """The stored copy, for somebody who has to open it to judge it.

    Two hard refusals and one soft one:

      infected  never. Not downloadable, not previewable. The point of catching
                it was to stop it reaching a person's machine, and a download
                button that hands it over anyway undoes the whole check.
      purged    the bytes are gone by retention. The record is still there; say
                so plainly rather than returning an empty file.
      no copy   an arrival from before its channel kept one.

    Every successful read is logged. These are files that failed inspection, and
    one day the question will be who looked at one.
    """
    if is_infected(arrival):
        raise ReviewError("This file failed the security scan and cannot be "
                          "downloaded.")
    if arrival.bytes_purged_at is not None:
        raise ReviewError(
            f"The file itself was deleted on "
            f"{arrival.bytes_purged_at.strftime('%d %b %Y')} under the "
            f"{retention_days()}-day retention rule. The record of it remains.")
    if not arrival.blob_ref:
        raise ReviewError("No copy of this file was kept.")

    data = storage.resolve_bytes(arrival.blob_ref, None)
    if data is None:
        raise ReviewError("The stored copy could not be read.")

    audit.log_access(str(user_id or "?"), f"intake/arrival/{arrival.public_ref}",
                     "download", ip=ip, user_id=user_id,
                     tenant_id=arrival.tenant_id)
    return data


# ── retention ───────────────────────────────────────────────────────────────

def purge_expired(session, *, now: Optional[datetime] = None) -> dict:
    """Delete the BYTES of old refused and held files. Keep every row.

    Only files that were not accepted. An accepted file's copy is what
    processing works from and is not this rule's business.

    A held file that nobody ever decided still ages out — the alternative is
    keeping a broker's data indefinitely because an operator was busy, which is
    the wrong way round. The row stays held, and the drawer will say the file
    itself is gone.
    """
    now = now or datetime.now(timezone.utc)
    normal_cutoff = now - timedelta(days=retention_days())
    quarantine_cutoff = now - timedelta(days=quarantine_retention_days())

    rows = (session.query(FileArrival)
            .filter(FileArrival.outcome != "accepted",
                    FileArrival.bytes_purged_at.is_(None),
                    FileArrival.blob_ref.isnot(None),
                    FileArrival.received_at < normal_cutoff)
            .limit(500).all())
    # Infected files go on a shorter clock, so they are collected separately
    # rather than waiting out the ninety days everything else gets.
    rows += (session.query(FileArrival)
             .filter(FileArrival.outcome != "accepted",
                     FileArrival.bytes_purged_at.is_(None),
                     FileArrival.blob_ref.isnot(None),
                     FileArrival.received_at < quarantine_cutoff,
                     FileArrival.turned_away_reason.like(
                         f"{safety.MALWARE_PREFIX}%"))
             .limit(500).all())

    purged, failed = 0, 0
    for arrival in rows:
        if arrival.bytes_purged_at is not None:
            continue                      # already collected by the first pass
        try:
            # Only Azure mode has a blob to delete. In DB mode store_or_keep
            # never hands back a ref at all, so a call here would be a pointless
            # round trip to a container that is not configured.
            if storage.is_azure():
                storage.delete_blob(arrival.blob_ref)
        except Exception as exc:
            # Stamp it anyway. A blob we cannot delete twice is a smaller
            # problem than a row that retries for ever.
            log.warning("could not delete %s: %s", arrival.blob_ref, exc)
            failed += 1
        arrival.blob_ref = None
        arrival.bytes_purged_at = now
        purged += 1
    session.commit()
    if purged:
        log.info("retention purged %s stored files (%s delete errors)",
                 purged, failed)
    return {"purged": purged, "delete_errors": failed}


def sweep_route_folders(session, *, now: Optional[datetime] = None) -> dict:
    """The same rule for the copies sitting on the SFTP filesystem.

    A file refused on SFTP is moved into `rejected`, `held` or `quarantine` and
    stays there. Those folders are what an operator actually browses, and left
    alone they grow for ever with exactly the data the purge above just went to
    the trouble of removing from storage.
    """
    import intake_service as svc
    from intake_models import IntakeRoute

    now = now or datetime.now(timezone.utc)
    removed, kept = 0, 0
    routes = (session.query(IntakeRoute)
              .filter(IntakeRoute.channel == "sftp").all())
    for route in routes:
        for folder, days in (("rejected", retention_days()),
                             ("held", retention_days()),
                             ("quarantine", quarantine_retention_days())):
            directory = svc.route_dir(route, folder)
            if not directory.is_dir():
                continue
            cutoff = now - timedelta(days=days)
            for path in directory.iterdir():
                if not path.is_file() or path.name.startswith("."):
                    continue
                try:
                    changed = datetime.fromtimestamp(
                        path.stat().st_mtime, tz=timezone.utc)
                    if changed < cutoff:
                        path.unlink()
                        removed += 1
                    else:
                        kept += 1
                except OSError as exc:
                    log.warning("could not sweep %s: %s", path, exc)
    return {"removed": removed, "kept": kept}


def run_retention() -> dict:
    """Both halves, in one call. What the daily task and the manual endpoint
    both go through, so testing it by hand exercises the real thing."""
    from db import SessionLocal
    with SessionLocal() as session:
        result = purge_expired(session)
        result.update(sweep_route_folders(session))
        return result


# ── the nightly task ────────────────────────────────────────────────────────

_task = None


def _retention_enabled() -> bool:
    """ON by default, unlike the pollers.

    A poller that is off collects nothing and nobody is worse off. Retention
    that is off keeps every broker file for ever, which is the outcome the rule
    exists to prevent — so the safe default is the opposite way round.
    """
    return os.getenv("INTAKE_RETENTION_ENABLED", "1").strip().lower() in (
        "1", "true", "yes", "on")


async def _loop() -> None:                        # pragma: no cover - wiring
    import asyncio
    from fastapi.concurrency import run_in_threadpool

    interval = _int_env("INTAKE_RETENTION_INTERVAL_HOURS", 24) * 3600
    log.info("intake retention started — every %sh, keeping refused files %s "
             "days (%s for quarantine)",
             interval // 3600, retention_days(), quarantine_retention_days())
    while True:
        # Sleep FIRST. A sweep at the moment of every deploy would mean a busy
        # release day spends its time deleting instead of serving.
        await asyncio.sleep(interval)
        try:
            result = await run_in_threadpool(run_retention)
            if result.get("purged") or result.get("removed"):
                log.info("retention: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("retention sweep failed: %s", exc)


def start(app) -> None:
    """Attach the sweep to the app, the same way the pollers do."""
    import asyncio

    if not _retention_enabled():
        log.warning("intake retention DISABLED — refused files will be kept "
                    "indefinitely (INTAKE_RETENTION_ENABLED=0)")
        return

    @app.on_event("startup")
    async def _start_retention() -> None:         # pragma: no cover - wiring
        global _task
        if _task is None or _task.done():
            _task = asyncio.create_task(_loop())

    @app.on_event("shutdown")
    async def _stop_retention() -> None:          # pragma: no cover - wiring
        global _task
        if _task is not None:
            _task.cancel()
            _task = None
