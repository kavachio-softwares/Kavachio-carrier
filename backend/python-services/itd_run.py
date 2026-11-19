"""ITD handling inside a bordereau run — where itd.py and itd_store.py meet
the run (direct_routes._prepare_run / _render_prepared).

  prepare()  just before the landing record is written: is ITD on for this
             setup? Is the file ITD? Compare it with the feed's running total
             and hand back the landing the run should process — for an ITD
             file only its new rows and adjustments, for a regular file the
             file exactly as it came.
  stage()    once the landing record has an id: the comparison is written
             as PENDING events.
  finish()   after the run has produced its output: the running total moves
             forward. Never raises — a failure here leaves the events pending,
             and the next file of the feed applies them before it compares.

Everything is a no-op for a setup whose ITD settings are off — which is every
setup until somebody switches them on — so the run is then exactly what it
was before ITD handling existed.

A corrected file: a broker re-sending a bordereau for the same period (after
exceptions, say) makes the next VERSION of the same submission, and only the
current version is ever delivered (submission_service.deliver). So a version
that was never delivered must not count as "already delivered" for the one
that replaces it — prepare() undoes its effect on the running total first
(set_aside_earlier_versions), and the new version is compared with what was
delivered before it. A delivered version is never undone: the carrier has it.

Why the landing holds only the delta: every later step reads the landing —
validation, the output BDX, Fix (which maps an output row back to its landing
row by position), Re-generate, and the data-model load. Storing the delta as
the landing keeps all of them unchanged and in agreement. The file as it came
is kept as the arrival's copy (never dropped for an ITD file) and every one of
its rows has an event saying what happened to it.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import itd
import itd_store

log = logging.getLogger("kavachio.itd")


class OlderItdFile(Exception):
    """An ITD file older than the latest one this feed has already loaded."""


class ItdHistoryUnavailable(Exception):
    """ITD is on for the setup, but the feed's history cannot be read where the
    app keeps it (the ITD tables are missing, or the old names are still there
    — migrations 39/40). Comparing anyway would treat every transaction as new
    and send it again, so the file is held."""


def _sheet_streams(landing: dict, cfg: dict) -> list[str]:
    names = list(((landing or {}).get("sheets") or {}).keys())
    out = []
    for n in names:
        sc = itd._sheet_cfg(cfg, n)
        if sc is not None:
            out.append(sc["stream"])
    return sorted(set(out))


# Versions the carrier has received — never undone by a later version.
DELIVERED = ("delivered", "delivered_flagged")


def set_aside_earlier_versions(s, arrival) -> list[dict]:
    """Undo what earlier, undelivered versions of this file's submission did
    to the running total (itd_store.withdraw), newest first. The caller
    commits. Returns one entry per version touched: {"version", "arrival_id",
    "undone"} — plus "error" when a version could not be undone because a
    later file of the feed has already been compared against it."""
    if arrival is None or not getattr(arrival, "submission_ref", None):
        return []
    from intake_models import FileArrival
    q = s.query(FileArrival).filter(
        FileArrival.tenant_id == arrival.tenant_id,
        FileArrival.submission_ref == arrival.submission_ref,
        FileArrival.id != arrival.id)
    if arrival.version_no is not None:
        q = q.filter(FileArrival.version_no < arrival.version_no)
    out: list[dict] = []
    for old in q.order_by(FileArrival.version_no.desc(), FileArrival.id.desc()).all():
        if (old.version_status or "") in DELIVERED or not itd_store.has_effect(s, old.id):
            continue
        try:
            n = itd_store.withdraw(s, old.id)
        except itd_store.CannotWithdraw as exc:
            out.append({"version": old.version_no, "arrival_id": old.id, "undone": 0,
                        "error": str(exc)})
            continue
        old.itd_summary = {**(old.itd_summary or {}), "status": "withdrawn",
                           "replaced_by": arrival.version_no}
        out.append({"version": old.version_no, "arrival_id": old.id, "undone": n})
    return out


def _replaced_text(replaced: list[dict], version_no: Optional[int]) -> str:
    done = [str(r["version"]) for r in replaced if "error" not in r]
    stuck = [str(r["version"]) for r in replaced if "error" in r]
    text = ""
    if done:
        many = len(done) > 1
        text += (f" This is version {version_no} of the submission: version"
                 f"{'s' if many else ''} {', '.join(done)} {'were' if many else 'was'} "
                 f"never delivered, so {'they were' if many else 'it was'} set aside "
                 f"and this file was compared with what was delivered before.")
    if stuck:
        text += (f" Version {', '.join(stuck)} could not be set aside — a later file "
                 f"has already been compared against it — so rows it brought count "
                 f"as already delivered.")
    return text


def apply_completed_pending(s, tenant_id: int, program_id: int, broker_id: int,
                            on_changed: str) -> int:
    """Apply the pending events of earlier runs of this feed that DID produce
    their output — runs whose finish() failed. A run that never produced an
    output (it failed) keeps its events pending for ever and is ignored."""
    from sqlalchemy import and_, select, text
    e = itd_store._ev()
    landing_ids = [r[0] for r in s.execute(
        select(e.c.comparison_landing_id).where(and_(
            e.c.tenant_id == tenant_id, e.c.comparison_program_id == program_id,
            e.c.comparison_broker_party_id == broker_id,
            e.c.comparison_status == itd_store.PENDING)).distinct()).all()]
    done = 0
    for lid in sorted(i for i in landing_ids if i):
        produced = s.execute(text("SELECT output_export_id FROM landing_record "
                                  "WHERE id = :id"), {"id": lid}).scalar()
        if not produced:
            continue
        arrival_id = s.execute(select(e.c.comparison_file_arrival_id).where(
            e.c.comparison_landing_id == lid).limit(1)).scalar()
        itd_store.apply(s, lid, arrival_id=arrival_id, on_changed=on_changed)
        done += 1
    return done


def prepare(s, *, tenant_id: int, pipe, program_id: Optional[int],
            broker_party_id: Optional[int], landing: dict,
            file_bytes: Optional[bytes] = None, arrival_id: Optional[int] = None,
            period: Optional[str] = None, check_only: bool = False
            ) -> tuple[dict, Optional[dict]]:
    """(landing to store, plan). The plan is None when ITD is off, and then the
    landing is returned untouched. Raises OlderItdFile for an ITD file older
    than one this feed has already loaded, and ItdHistoryUnavailable when ITD
    is on but the feed's history cannot be read."""
    if pipe is None:
        return landing, None
    try:
        present = itd_store.tables_present(s)
        cfg = itd_store.read_config(s, pipe.id)
    except Exception:  # noqa: BLE001 — ITD must never be why a run cannot start
        log.warning("ITD settings could not be read for setup %s", pipe.id, exc_info=True)
        return landing, None
    if not itd.enabled(cfg):
        return landing, None
    # ITD is on: without the feed's history every transaction would look new
    # and be sent again — hold the file instead of comparing it with nothing.
    left = itd_store.old_tables_left(s)
    if not present or left:
        log.error("ITD file held for setup %s: history unavailable (tables present: %s, "
                  "old names left: %s)", pipe.id, present, left)
        raise ItdHistoryUnavailable(
            "Not processed — this setup handles ITD files, and its earlier ITD "
            "history can't be read right now, so the file can't be compared safely. "
            "Nothing was sent. Please try again once support confirms ITD is available.")
    # Automatic settings take up amount columns recognised since they were made.
    cfg = itd.refresh_money_columns(cfg, landing)
    streams = _sheet_streams(landing, cfg)
    if not streams:
        return landing, None            # none of the configured sheets came in

    arrival = None
    file_period = itd.label_period(period)
    if arrival_id:
        from intake_models import FileArrival
        arrival = s.get(FileArrival, arrival_id)
        if arrival is not None and not file_period:
            file_period = itd.label_period(arrival.reporting_period)

    det = itd.detect(landing, cfg, file_period)
    load_type = itd.ITD if det["is_itd"] else itd.REGULAR
    broker = broker_party_id if broker_party_id is not None else pipe.broker_party_id
    program_id, broker_id = itd_store.feed_of(program_id or pipe.program_id, broker)

    replaced: list[dict] = []
    aside = None
    if not check_only:
        # In a savepoint: a file refused below as an older ITD must leave the
        # versions it would have replaced exactly as they were.
        aside = s.begin_nested()
        replaced = set_aside_earlier_versions(s, arrival)
        apply_completed_pending(s, tenant_id, program_id, broker_id, cfg["on_changed"])
    if load_type == itd.ITD:
        last = itd_store.last_itd_period(s, tenant_id, program_id, broker_id)
        if last and det["as_at"] and det["as_at"] < last:
            # Plain months ("July 2025") for the person reading it. Not loaded:
            # an older ITD would wind this feed's figures back.
            month = lambda p: datetime.strptime(p, "%Y-%m").strftime("%B %Y")  # noqa: E731
            msg = (f"Not processed — this file covers up to {month(det['as_at'])}, "
                   f"but we already have your ITD file up to {month(last)}. "
                   f"Please send your latest ITD file.")
            if not check_only:
                aside.rollback()
                _mark_arrival(arrival, load_type, det, {"held": "older_itd",
                                                       "last_itd": last})
                s.commit()
                raise OlderItdFile(msg)
    if aside is not None:
        aside.commit()
    if replaced:
        det["reason"] = (det["reason"] or "") + _replaced_text(
            replaced, getattr(arrival, "version_no", None))

    positions = itd_store.load_positions(s, tenant_id, program_id, broker_id, streams)
    comparison = itd.compare(landing, cfg, positions, load_type=load_type)
    delta = itd.build_delta(landing, cfg, comparison, load_type=load_type,
                            as_at=det["as_at"])
    summary = comparison["summary"]
    # Rows whose transaction date is before the period they are reported in —
    # late transactions; loaded like any other row (itd.detect).
    summary["late_rows"] = det["late_rows"]
    if replaced:
        summary["replaced_versions"] = [r["version"] for r in replaced
                                        if "error" not in r]
    delta["itd"].update(reason=det["reason"], periods=det["periods"],
                        transaction_periods=det["transaction_periods"],
                        check_only=check_only)

    if arrival is not None:
        _mark_arrival(arrival, load_type, det, {**summary, "status": "pending"})
        if load_type == itd.ITD and file_bytes and not arrival.blob_ref:
            # A hand upload keeps no copy of its own when there is no blob
            # storage. An ITD file's landing holds only its delta, so the file
            # as it came must be kept here (and is never dropped — see
            # intake_service.mark_run).
            import intake_service
            intake_service.keep_copy(s, arrival, file_bytes)

    plan = {
        "tenant_id": tenant_id, "program_id": program_id, "broker_id": broker_id,
        "arrival_id": arrival_id, "load_type": load_type, "as_at": det["as_at"],
        "reason": det["reason"], "summary": summary, "on_changed": cfg["on_changed"],
        "check_only": check_only, "results": comparison["results"],
    }
    return delta, plan


def _mark_arrival(arrival, load_type: str, det: dict, summary: dict) -> None:
    if arrival is None:
        return
    start, end = itd.period_bounds(det.get("periods") or [])
    arrival.load_type = load_type
    arrival.load_type_reason = det.get("reason")
    arrival.period_start = start
    arrival.period_end = end
    arrival.itd_summary = summary


def stage(s, plan: Optional[dict], landing_id: int) -> None:
    """Write the plan's comparison as events on the new landing. The caller
    commits. Drops the per-row results from the plan once written."""
    if not plan:
        return
    itd_store.stage(
        s, tenant_id=plan["tenant_id"], program_id=plan["program_id"],
        broker_id=plan["broker_id"], arrival_id=plan["arrival_id"],
        landing_id=landing_id, load_type=plan["load_type"], period=plan["as_at"],
        results=plan.pop("results", []), check_only=plan["check_only"])


def public(plan: Optional[dict]) -> Optional[dict]:
    """What a run's result says about ITD."""
    if not plan:
        return None
    return {k: plan.get(k) for k in ("load_type", "as_at", "reason", "summary",
                                     "check_only", "applied", "conflicts",
                                     "apply_error")}


def finish(landing_id: int, plan: Optional[dict]) -> Optional[dict]:
    """After the run produced its output: move the running total forward and
    record the outcome on the arrival. Never raises."""
    if not plan:
        return None
    if plan.get("check_only"):
        plan["applied"], plan["conflicts"] = 0, 0
        return public(plan)
    from db import SessionLocal
    try:
        with SessionLocal() as s:
            res = itd_store.apply(s, landing_id, arrival_id=plan.get("arrival_id"),
                                  on_changed=plan.get("on_changed") or "adjust")
            plan.update(res)
            if plan.get("arrival_id"):
                from intake_models import FileArrival
                a = s.get(FileArrival, plan["arrival_id"])
                if a is not None:
                    a.itd_summary = {**(plan.get("summary") or {}), "status": "applied",
                                     **res}
            s.commit()
    except Exception as e:  # noqa: BLE001 — the output exists; the next file heals this
        log.warning("ITD running total not moved for landing %s: %s", landing_id, e,
                    exc_info=True)
        plan["apply_error"] = ("The running total could not be updated; it will be "
                               "updated before the next file for this feed is compared.")
    return public(plan)
