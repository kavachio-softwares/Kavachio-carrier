"""Auto-run — a file that arrived and passed its checks is run against its contract.

Until this existed an accepted file (email, SFTP, API) was checked, stored and
then left: `file_arrival.bdx_upload_id` was reserved for "the processing seam"
and never written, and the Files screen could only ever say "Waiting to be
run". This worker is that seam.

Every few seconds it takes accepted arrivals nobody has run yet
(`run_state IS NULL`) and runs each one through exactly the path a person
uploading on Process Bordereau uses — `direct_routes._prepare_run` then
`_render_prepared` — so an automatic run and a hand run can never disagree.
The result is written back on the arrival (run_landing_id, run_export_id,
run_state, run_error), which is what the Files screen reads.

What it will NOT run, and says so on the arrival (`not_run` + run_error):
  · a file on a way in that names no programme — which setup would it use?
  · a file whose stored copy is gone
  · a programme with no live setup yet

Old files are safe: migration 29 stamps every arrival accepted before this
existed as `pre_autorun`, so switching it on never runs a backlog.

A claim is an UPDATE ... WHERE run_state IS NULL, so two app instances can
never run the same file twice. Opt out with INTAKE_AUTORUN_ENABLED=0.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Optional

from sqlalchemy import func, or_, update

log = logging.getLogger("kavachio.intake.autorun")

_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_wake = threading.Event()

ACTOR = "Auto-run"


def _enabled() -> bool:
    return os.environ.get("INTAKE_AUTORUN_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _every_seconds() -> int:
    try:
        return max(5, int(os.environ.get("INTAKE_AUTORUN_SECONDS", "10")))
    except ValueError:
        return 10


def wake() -> None:
    """Look now rather than at the next tick — e.g. right after a release."""
    _wake.set()


# ── one file ────────────────────────────────────────────────────────────────

def _claim(s, arrival_id: int) -> bool:
    from intake_models import FileArrival
    res = s.execute(update(FileArrival)
                    .where(FileArrival.id == arrival_id,
                           FileArrival.run_state.is_(None))
                    .values(run_state="running"))
    s.commit()
    return res.rowcount == 1


def _live_contract_id(s, tenant_id: int, program_id: int,
                      broker_party_id: Optional[int]) -> Optional[int]:
    """The one contract this file is written under, when there is exactly one.

    Same eligibility rule as the arrival's own live-contract check. With two or
    more the setup's own contracts govern (None), exactly as a hand run that
    names no contract behaves — guessing between two binders would measure the
    file against the wrong terms."""
    from db import Contract, Program
    from contract_upload_services.contract_asof import NON_GOVERNING_STATUSES
    q = (s.query(Contract.id)
         .join(Program, Program.id == Contract.program_id)
         .filter(Program.tenant_id == tenant_id,
                 Contract.program_id == program_id,
                 func.coalesce(Contract.status, "").notin_(NON_GOVERNING_STATUSES)))
    if broker_party_id is not None:
        q = q.filter(or_(Contract.broker_party_id == broker_party_id,
                         Contract.broker_party_id.is_(None)))
    ids = [r[0] for r in q.limit(2).all()]
    return ids[0] if len(ids) == 1 else None


def _live_pipeline(s, tenant_id: int, program_id: int, broker_party_id: Optional[int]):
    """This broker's live setup first, then the programme-wide one."""
    from db import Pipeline
    base = (s.query(Pipeline)
            .filter(Pipeline.tenant_id == tenant_id,
                    Pipeline.program_id == program_id,
                    Pipeline.status == "active"))
    if broker_party_id is not None:
        own = (base.filter(Pipeline.broker_party_id == broker_party_id)
               .order_by(Pipeline.id.desc()).first())
        if own is not None:
            return own
    return (base.filter(Pipeline.broker_party_id.is_(None))
            .order_by(Pipeline.id.desc()).first())


def _detail(e: BaseException) -> str:
    d = getattr(e, "detail", None)
    if isinstance(d, dict):
        d = d.get("message") or d.get("detail") or str(d)
    return str(d or e) or e.__class__.__name__


def run_one(arrival_id: int) -> None:
    """Run one claimed arrival and record the outcome on it."""
    import intake_service as svc
    from db import SessionLocal
    from intake_models import FileArrival, IntakeRoute

    with SessionLocal() as s:
        a = s.get(FileArrival, arrival_id)
        if a is None:
            return
        route = s.get(IntakeRoute, a.route_id) if a.route_id else None
        tenant_id = a.tenant_id
        program_id = (getattr(route, "program_id", None) if route else None) or a.program_id
        broker = a.matched_broker_party_id
        filename, blob_ref = a.filename, a.blob_ref
        # land_file ticks the calendar for a file accepted on a programme's way
        # in — a route pinned to the programme, or a file that named it (its
        # programme is then on the arrival). A held file somebody released was
        # never ticked, so its run does it.
        calendar_done = bool(route and program_id and a.resolution != "released")
        # Which period that run ticks. A released file that re-sends or
        # corrects a submission is for its submission's period; left to guess,
        # the run looked for the oldest period still open, found every ended
        # one already received, and recorded nothing — so the calendar was a
        # version short of the file history.
        run_period = None
        if a.resolution == "released":
            import submission_service
            run_period = submission_service.period_of(s, a)

        if program_id is None:
            svc.mark_run(arrival_id, state="not_run", error=(
                "This channel is not linked to a programme, so there is no setup "
                "to process it with. Link the channel to a programme, or process "
                "the file manually."))
            return
        pipe = _live_pipeline(s, tenant_id, program_id, broker)
        if pipe is None:
            svc.mark_run(arrival_id, state="not_run", error=(
                "There is no live setup for this programme yet. Activate one on "
                "the Setup page, then run the file by hand."))
            return
        carrier_party_id = pipe.carrier_party_id
        # The contract the file is written under, as it arrived (picked, sent,
        # named, or the only one) — else the old rule: the only live one.
        contract_id = (getattr(a, "contract_id", None)
                       or _live_contract_id(s, tenant_id, program_id, broker))

    data = svc.read_copy(blob_ref, arrival_id)
    if not data:
        svc.mark_run(arrival_id, state="not_run",
                     error="No copy of this file was kept, so it cannot be run.")
        return

    import direct_routes as dr

    async def _go() -> dict:
        prep = await dr._prepare_run(
            tenant_id, carrier_party_id=carrier_party_id, program_id=program_id,
            file_bytes=data, source_filename=filename, skip_rows=0,
            broker_party_id=broker, contract_id=contract_id)
        # Recorded now, so a render that fails still points at its landing.
        svc.mark_run(arrival_id, state="running", landing_id=prep["landing_id"])
        # Data the broker sent from the secure correction link carries their
        # earlier Approve / Dismiss decisions onto this landing before it is
        # checked. A no-op for every other file.
        import submission_service
        submission_service.carry_decisions(arrival_id, prep["landing_id"])
        result = await dr._render_prepared(prep, filename=None, actor=ACTOR,
                                           mark_calendar=not calendar_done,
                                           period=run_period)
        result["_landing_id"] = prep["landing_id"]
        return result

    try:
        result = asyncio.run(_go())
    except Exception as e:  # noqa: BLE001 — every failure is recorded, never raised
        log.info("auto-run of arrival %s failed: %s", arrival_id, _detail(e))
        svc.mark_run(arrival_id, state="failed", error=_detail(e))
        return
    svc.mark_run(arrival_id, state="done", landing_id=result.get("_landing_id"),
                 export_id=result.get("export_id"))
    log.info("auto-ran arrival %s → export %s (%s)", arrival_id,
             result.get("export_id"), result.get("status"))


# ── the loop ────────────────────────────────────────────────────────────────

def tick(limit: int = 5) -> int:
    """Run up to `limit` waiting arrivals. Returns how many were attempted."""
    from db import SessionLocal
    from intake_models import FileArrival
    with SessionLocal() as s:
        ids = [r[0] for r in (s.query(FileArrival.id)
                              .filter(FileArrival.outcome == "accepted",
                                      FileArrival.run_state.is_(None))
                              .order_by(FileArrival.id)
                              .limit(limit).all())]
        claimed = [i for i in ids if _claim(s, i)]
    for i in claimed:
        if _stop.is_set():
            break
        try:
            run_one(i)
        except Exception:  # noqa: BLE001 — one bad file must not stop the loop
            log.exception("auto-run of arrival %s crashed", i)
            import intake_service as svc
            svc.mark_run(i, state="failed", error="The run stopped unexpectedly.")
    return len(claimed)


def _run() -> None:
    log.info("auto-run on — accepted files are run every %ss", _every_seconds())
    while not _stop.is_set():
        try:
            busy = tick()
        except Exception:  # noqa: BLE001 — e.g. the DB is briefly unreachable
            log.warning("auto-run tick failed", exc_info=True)
            busy = 0
        if busy:
            continue            # more may be waiting; look again straight away
        _wake.wait(_every_seconds())
        _wake.clear()


def start(app) -> None:
    """Attach the worker to the app's startup, the same way sftp_poller does."""
    if not _enabled():
        log.info("auto-run off (INTAKE_AUTORUN_ENABLED=0) — accepted files wait "
                 "to be run by hand")
        return

    @app.on_event("startup")
    async def _start_autorun() -> None:    # pragma: no cover - wiring
        global _thread
        if _thread is None or not _thread.is_alive():
            _stop.clear()
            _thread = threading.Thread(target=_run, name="intake-autorun", daemon=True)
            _thread.start()

    @app.on_event("shutdown")
    async def _stop_autorun() -> None:     # pragma: no cover - wiring
        _stop.set()
        _wake.set()
