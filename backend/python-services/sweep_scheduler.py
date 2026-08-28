"""C-9 — daily background sweep (in-process, no external cron).

Runs `submission_calendar_service.sweep_overdue` across ALL tenants once a day so
the overdue / due-soon reminder events that feed the notification bell get created
even when nobody opens the calendar. Until now the sweep only fired lazily on a
calendar view or a manual POST /calendar/sweep.

Safe by design:
  * Idempotent — the sweep guards each reminder with overdue_notified /
    due_soon_notified, so running daily (or more often, or from several workers)
    never double-sends. Extra runs simply find nothing new.
  * Off the event loop — the (synchronous) DB work runs in a threadpool, so it
    never blocks request handling.
  * Fail-safe — a failed run is logged and retried next cycle; it never crashes
    the app or the loop.

Configuration (all optional, sensible defaults):
  * SWEEP_SCHEDULER_ENABLED  on/off        (default on; set 0/false to disable)
  * SWEEP_HOUR_UTC           0..23         (default 6  → 06:00 UTC daily)
  * SWEEP_STARTUP_DELAY_S    seconds       (default 20 → one catch-up run shortly
                                            after boot so a fresh deploy doesn't
                                            wait until the next run hour)
  * SWEEP_EMAIL_ENABLED      on/off        (default on) — the C-9 deadline digest
                                            to each tenant's admins. Turn OFF to
                                            keep the in-app bell/card but send no
                                            mail. Requires NOTIFY_SMTP_* (or the
                                            default SMTP_*) and APP_BASE_URL.

If the app runs with multiple workers/replicas, each will run its own scheduler.
That is harmless (idempotent), but you can set SWEEP_SCHEDULER_ENABLED=0 on the
extra workers to avoid the redundant runs.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

log = logging.getLogger("sweep_scheduler")

_task: asyncio.Task | None = None


def _enabled() -> bool:
    return os.getenv("SWEEP_SCHEDULER_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off", "",
    )


def _run_hour() -> int:
    try:
        return max(0, min(23, int(os.getenv("SWEEP_HOUR_UTC", "6"))))
    except ValueError:
        return 6


def _startup_delay() -> float:
    try:
        return max(0.0, float(os.getenv("SWEEP_STARTUP_DELAY_S", "20")))
    except ValueError:
        return 20.0


def _seconds_until_next_run(now: datetime, hour: int) -> float:
    """Seconds from `now` (tz-aware UTC) to the next `hour`:00 UTC."""
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _emails_enabled() -> bool:
    return os.getenv("SWEEP_EMAIL_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off", "",
    )


# Brand prefix on the subject line: "Kavachio - 2 bordereaux overdue". A
# deadline reminder lands in an inbox with no surrounding context — the bare
# headline reads like spam, and a broker admin works across several platforms.
# A plain hyphen, not an em dash: some clients and mobile previews render the
# dash inconsistently, and subject lines are matched on by inbox rules.
SUBJECT_PREFIX = "Kavachio - "


def _digest(rows: list[dict]) -> tuple[str, list[tuple[str, str]]]:
    """Subject + ordered (label, value) facts for one tenant's digest.

    ONE message per tenant per run, not one per period: a single sweep can turn
    five periods late at once, and five separate emails for one event is the
    nagging Group 3 explicitly rules out.
    """
    overdue = [r for r in rows if r["kind"] == "overdue"]
    due_today = [r for r in rows if r["kind"] == "due_today"]
    due_soon = [r for r in rows if r["kind"] == "due_soon"]
    parts = []
    if overdue:
        parts.append(f"{len(overdue)} bordereau{'' if len(overdue) == 1 else 'x'} overdue")
    if due_today:
        parts.append(f"{len(due_today)} due today")
    if due_soon:
        parts.append(f"{len(due_soon)} due soon")
    subject = SUBJECT_PREFIX + (" · ".join(parts) or "Submission deadlines")

    # Overdue first, then due-today, then due-soon; within each, earliest
    # deadline first — the same priority order the in-app card and bell use, so a
    # broker reading the email and then opening the app sees the same sequence.
    facts: list[tuple[str, str]] = []
    for label, group in (("Overdue", overdue), ("Due today", due_today),
                         ("Due soon", due_soon)):
        for r in sorted(group, key=lambda x: (x["due_date"] or "", x["program_name"])):
            facts.append((f"{label} — {r['program_name']}",
                          f"{r['period']} · due {r['due_date']}"))
    return subject, facts


def _send_tenant_digest(tenant_id: int, rows: list[dict]) -> bool:
    """Mail one tenant's admins. Returns True only if at least one message went
    out, so the caller knows whether it may mark these rows emailed.

    A tenant with no active admins is NOT a failure — there is simply nobody to
    tell. Those rows are still marked emailed, otherwise every run would rebuild
    and re-attempt the same never-deliverable digest forever.
    """
    from notifications import (NOTIFY_MAIL_ACCOUNT, TENANT_ADMIN_FOOTER,
                               _app_link, _email_text,
                               notification_email_html, tenant_admin_recipients)
    people = tenant_admin_recipients(tenant_id)
    if not people:
        log.info("C-9 email: tenant %s has no active tenant_admin — nothing to send",
                 tenant_id)
        return True

    subject, facts = _digest(rows)
    link = _app_link("/calendar")
    action = "Upload the outstanding bordereaux, or adjust the schedule if the deadline is wrong."
    text = _email_text(subject, None, facts, link, action)

    sent = 0
    try:
        from email_utils import send_email
    except Exception as e:  # noqa: BLE001
        log.warning("C-9 email: transport unavailable (%s) — will retry next run", e)
        return False
    for person in people:
        try:
            send_email(
                person["email"], subject,
                notification_email_html(subject, None, facts, link,
                                        "Open My Calendar",
                                        greeting_name=person.get("name"),
                                        action=action,
                                        # These recipients are the BROKER's own
                                        # admins, not Kavachio staff — the
                                        # default footer would tell them
                                        # otherwise and read as a misrouted mail.
                                        footer=TENANT_ADMIN_FOOTER),
                text=text, account=NOTIFY_MAIL_ACCOUNT,
            )
            sent += 1
        except Exception as e:  # noqa: BLE001
            # One bad address must not cost the other recipients their reminder.
            log.warning("C-9 email to %s failed: %s", person["email"], e)
    return sent > 0


def _sweep_all_tenants() -> dict:
    """Synchronous DB work — runs in a threadpool. Sweeps every tenant
    (tenant_id=None) and commits. Imports are local to dodge import cycles.

    Then sends the C-9 deadline digest. Mail is sent HERE and nowhere else:
    sweep_overdue() also runs lazily when someone opens My Calendar, and mailing
    from inside it would notify the admins on every page view.
    """
    from db import SessionLocal
    from submission_calendar_service import (mark_emailed, pending_email_rows,
                                             sweep_overdue)
    with SessionLocal() as s:
        result = sweep_overdue(s, tenant_id=None)   # None → all tenants
        s.commit()

        if not _emails_enabled():
            return {**result, "emailed_tenants": 0}

        by_tenant: dict[int, list[dict]] = {}
        for row in pending_email_rows(s):
            if row["tenant_id"] is not None:
                by_tenant.setdefault(row["tenant_id"], []).append(row)

        emailed = 0
        for tenant_id, rows in by_tenant.items():
            try:
                ok = _send_tenant_digest(tenant_id, rows)
            except Exception:  # noqa: BLE001
                log.exception("C-9 email: digest for tenant %s failed", tenant_id)
                ok = False
            if ok:
                # Only on success — a mail outage leaves the flags alone so the
                # next run retries instead of silently dropping the reminder.
                mark_emailed(s, rows)
                s.commit()
                emailed += 1
            else:
                s.rollback()
        return {**result, "emailed_tenants": emailed}


async def _loop() -> None:
    hour = _run_hour()
    log.info("C-9 sweep scheduler on — daily at %02d:00 UTC", hour)
    try:
        # One catch-up run shortly after boot so reminders are fresh on deploy.
        await asyncio.sleep(_startup_delay())
        while True:
            try:
                loop = asyncio.get_running_loop()
                r = await loop.run_in_executor(None, _sweep_all_tenants)
                log.info("C-9 sweep: %s newly overdue, %s newly due-today, "
                         "%s newly due-soon, %s tenant(s) emailed",
                         r.get("newly_late"), r.get("newly_due_today"),
                         r.get("newly_due_soon"), r.get("emailed_tenants"))
            except Exception:  # noqa: BLE001 — a bad run must not kill the loop
                log.exception("C-9 sweep run failed; will retry next cycle")
            # Sleep until the next scheduled hour, then run again.
            await asyncio.sleep(_seconds_until_next_run(datetime.now(timezone.utc), hour))
    except asyncio.CancelledError:
        log.info("C-9 sweep scheduler stopped")
        raise


def start(app) -> None:
    """Register the scheduler on a FastAPI app's startup/shutdown. No-op when
    disabled via SWEEP_SCHEDULER_ENABLED."""
    if not _enabled():
        log.info("C-9 sweep scheduler disabled (SWEEP_SCHEDULER_ENABLED)")
        return

    @app.on_event("startup")
    async def _start_sweep() -> None:  # pragma: no cover - wiring
        global _task
        if _task is None or _task.done():
            _task = asyncio.create_task(_loop())

    @app.on_event("shutdown")
    async def _stop_sweep() -> None:   # pragma: no cover - wiring
        global _task
        if _task is not None:
            _task.cancel()
            try:
                await _task
            except asyncio.CancelledError:
                pass
            _task = None
