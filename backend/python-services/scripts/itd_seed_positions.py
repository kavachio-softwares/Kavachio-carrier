"""Seed a feed's ITD running total from the files it already ran — BEFORE its
setup's ITD settings are switched on.

WHY
---
ITD handling loads only what a feed has not delivered yet. A feed that has
been running for months has delivered plenty, but its running total
(bdx_sent_transaction) starts empty — so its first ITD file would look entirely
new and every earlier transaction would be counted twice. This replays the
feed's earlier runs, oldest first, through the same comparison a live run
uses (itd.py / itd_store.py) and records what they delivered. It loads
nothing, sends nothing and changes no run.

Usage (after migrations/38_itd_handling.sql, with the setup's ITD settings
filled in — the mode may still be "Off"):

    cd backend/python-services
    source .venv/bin/activate
    python -m scripts.itd_seed_positions --pipeline 12            # dry run
    python -m scripts.itd_seed_positions --pipeline 12 --apply    # write it

--broker limits it to one broker of a programme-wide setup. Safe to re-run:
a run that already has ITD events is skipped.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

from sqlalchemy import func, select  # noqa: E402

import itd  # noqa: E402
import itd_run  # noqa: E402
import itd_store  # noqa: E402
from db import LandingRecord, OutputExport, Pipeline, SessionLocal  # noqa: E402


def _runs(s, pipe, broker: int | None) -> list[tuple[LandingRecord, int]]:
    """(landing, broker id) for every completed run of the setup's programme,
    oldest first — the runs a person or auto-run produced an output from."""
    q = (s.query(LandingRecord, OutputExport.broker_party_id)
         .join(OutputExport, OutputExport.id == LandingRecord.output_export_id)
         .filter(LandingRecord.tenant_id == pipe.tenant_id,
                 OutputExport.program_id == pipe.program_id))
    if broker is not None:
        q = q.filter(func.coalesce(OutputExport.broker_party_id, 0) == broker)
    elif pipe.broker_party_id is not None:
        q = q.filter(OutputExport.broker_party_id == pipe.broker_party_id)
    return [(rec, int(b or pipe.broker_party_id or 0))
            for rec, b in q.order_by(LandingRecord.id).all()]


def _arrival_of(s, landing_id: int):
    from intake_models import FileArrival
    a = s.query(FileArrival.id).filter(FileArrival.run_landing_id == landing_id).first()
    return a[0] if a else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pipeline", type=int, required=True, help="Bordereau Setup (pipeline) id")
    ap.add_argument("--broker", type=int, default=None,
                    help="only this broker's runs (0 = runs with no broker)")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args()

    with SessionLocal() as s:
        pipe = s.get(Pipeline, args.pipeline)
        if pipe is None:
            sys.exit(f"no setup {args.pipeline}")
        if not itd_store.tables_present(s):
            sys.exit("migration 38 has not been run on this database")
        cfg = itd_store.read_config(s, pipe.id)
        if not cfg or not cfg.get("sheets"):
            sys.exit("fill in the setup's ITD settings (sheets and key columns) first")
        detect_cfg = {**cfg, "mode": "auto" if cfg["mode"] == "never" else cfg["mode"]}
        e = itd_store._ev()
        seen = {r[0] for r in s.execute(select(e.c.comparison_landing_id).distinct()).all()}

        totals = {"runs": 0, "skipped": 0, **{k: 0 for k in itd.RESULTS}}
        for rec, broker_id in _runs(s, pipe, args.broker):
            data = rec.data or {}
            if rec.id in seen or data.get("itd"):
                totals["skipped"] += 1
                continue
            streams = itd_run._sheet_streams(data, cfg)
            if not streams:
                totals["skipped"] += 1
                continue
            program_id, broker_id = itd_store.feed_of(pipe.program_id, broker_id)
            det = itd.detect(data, detect_cfg)
            load_type = itd.ITD if det["is_itd"] else itd.REGULAR
            positions = itd_store.load_positions(s, pipe.tenant_id, program_id,
                                                 broker_id, streams)
            cmp = itd.compare(data, cfg, positions, load_type=load_type)
            arrival_id = _arrival_of(s, rec.id)
            itd_store.stage(s, tenant_id=pipe.tenant_id, program_id=program_id,
                            broker_id=broker_id, arrival_id=arrival_id,
                            landing_id=rec.id, load_type=load_type,
                            period=det["as_at"], results=cmp["results"],
                            check_only=False)
            itd_store.apply(s, rec.id, arrival_id=arrival_id, on_changed=cfg["on_changed"])
            sm = cmp["summary"]
            totals["runs"] += 1
            for k in itd.RESULTS:
                totals[k] += sm.get(k, 0)
            print(f"run {rec.id:>7}  {load_type:<7}  as at {det['as_at'] or '?':<7}  "
                  f"broker {broker_id:<6}  {rec.source_filename or ''}\n"
                  f"          new {sm[itd.NEW]}, reversal {sm[itd.REVERSAL]}, "
                  f"unchanged {sm[itd.UNCHANGED]}, "
                  f"changed {sm[itd.CHANGED]}, details {sm[itd.DETAILS_CHANGED]}, "
                  f"dropped {sm[itd.DROPPED]}, duplicate {sm[itd.DUPLICATE]}, "
                  f"no key {sm[itd.MISSING_KEY]}")
        print(f"\n{totals['runs']} run(s) replayed, {totals['skipped']} skipped. "
              f"Transactions recorded: {totals[itd.NEW] + totals[itd.REVERSAL]}.")
        if args.apply:
            s.commit()
            print("Written.")
        else:
            s.rollback()
            print("Dry run — nothing written. Re-run with --apply to write.")


if __name__ == "__main__":
    main()
