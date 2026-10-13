"""ITD handling — the screens' API (itd.py, itd_store.py, itd_run.py).

  GET  /itd/pipelines/{id}/config           a setup's ITD settings + a suggestion
  PUT  /itd/pipelines/{id}/config           save them
                                            (no screen uses these two — a setup's
                                            settings are filled in automatically
                                            when it is created; they are for
                                            support to read or correct them)
  GET  /itd/arrivals/{id}/rows              a file's row-by-row comparison
  POST /itd/arrivals/{id}/withdraw          undo what a file did to the running total
  GET  /itd/feeds/positions                 a feed's running total
  POST /itd/positions/{id}/decision         decide on a transaction an ITD dropped
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import itd
import itd_store
from app_routes import _actor, _log, _tenant_name, assert_tenant_owns, resolve_tenant_id
from auth_deps import Principal, current_principal, require_role
from db import Pipeline, SessionLocal

router = APIRouter(prefix="/itd", tags=["itd"])


def _need_tables(s) -> None:
    if not itd_store.tables_present(s):
        raise HTTPException(409, "ITD handling needs migration 38 on this database.")


@router.get("/pipelines/{pipeline_id}/config")
def get_config(pipeline_id: int, principal: Principal = Depends(current_principal)):
    """A setup's ITD settings (for a setup created since ITD was added, the
    ones filled in automatically — config.auto_filled), its input sheets and
    headings, and a suggested starting point. For support; no screen calls it."""
    with SessionLocal() as s:
        p = s.get(Pipeline, pipeline_id)
        if not p:
            raise HTTPException(404, "setup not found")
        assert_tenant_owns(principal, p.tenant_id)
        layout = itd_store.setup_layout(s, p)
        return {
            "pipeline_id": p.id,
            "available": itd_store.config_column_present(s) and itd_store.tables_present(s),
            "config": itd_store.read_config(s, p.id),
            "sheets": [{"sheet": n, "columns": cols} for n, cols in layout],
            "suggestion": itd.suggest_config(layout),
        }


class ConfigBody(BaseModel):
    config: Optional[dict] = None


@router.put("/pipelines/{pipeline_id}/config")
def put_config(pipeline_id: int, body: ConfigBody,
               principal: Principal = Depends(require_role("carrier_admin"))):
    """Save a setup's ITD settings. An empty or missing config switches ITD off."""
    with SessionLocal() as s:
        p = s.get(Pipeline, pipeline_id)
        if not p:
            raise HTTPException(404, "setup not found")
        assert_tenant_owns(principal, p.tenant_id)
        cfg = itd.normalize_config(body.config) if body.config else None
        if cfg and cfg["mode"] != "never" and not cfg["sheets"]:
            raise HTTPException(400, "Pick at least one sheet and its transaction id "
                                     "(or fallback) columns before switching ITD on.")
        if cfg:
            # Saved by a person now — no longer the automatic settings, so a
            # later change of input template leaves them alone.
            cfg["auto_filled"] = False
        try:
            stored = itd_store.write_config(s, p.id, cfg)
        except LookupError:
            raise HTTPException(409, "This setup's ITD settings cannot be saved yet: "
                                     "the database is missing migration 38.")
        s.commit()
        _log(_tenant_name(s, p.tenant_id) or "", _actor(principal), "itd_config_saved",
             target=f"pipeline:{p.id}",
             details={"mode": (stored or {}).get("mode", "never"),
                      "sheets": sorted(((stored or {}).get("sheets") or {}).keys())},
             principal=principal)
        return {"pipeline_id": p.id, "config": stored}


def _arrival(s, arrival_id: int, principal: Principal):
    from intake_models import FileArrival
    a = s.get(FileArrival, arrival_id)
    if a is None:
        raise HTTPException(404, "no such file")
    assert_tenant_owns(principal, a.tenant_id)
    return a


@router.get("/arrivals/{arrival_id}/rows")
def arrival_rows(arrival_id: int, result: Optional[str] = Query(default=None),
                 cls: Optional[str] = Query(default=None, alias="class"),
                 limit: int = Query(default=200, ge=1, le=1000),
                 offset: int = Query(default=0, ge=0),
                 principal: Principal = Depends(current_principal)):
    """What happened to each row of a file, in the client's classes (New,
    Previously processed, Changed, Cancelled or reversed — plus Skipped, key held_back) and
    the result underneath (with the money difference, the changed columns, the
    transaction a reversal reverses). Narrow with ?class= or ?result=."""
    if result and result not in itd.RESULTS:
        raise HTTPException(400, f"result must be one of {', '.join(itd.RESULTS)}")
    if cls and cls not in itd.CLASSES:
        raise HTTPException(400, f"class must be one of {', '.join(itd.CLASSES)}")
    with SessionLocal() as s:
        _need_tables(s)
        a = _arrival(s, arrival_id, principal)
        out = itd_store.file_rows(s, a.tenant_id, arrival_id=a.id, result=result,
                                  cls=cls, limit=limit, offset=offset)
        out.update({"arrival_id": a.id, "filename": a.filename, "load_type": a.load_type,
                    "load_type_reason": a.load_type_reason,
                    "period_start": a.period_start.isoformat() if a.period_start else None,
                    "period_end": a.period_end.isoformat() if a.period_end else None,
                    "summary": a.itd_summary})
        return out


@router.post("/arrivals/{arrival_id}/withdraw")
def withdraw_arrival(arrival_id: int,
                     principal: Principal = Depends(require_role("carrier_admin"))):
    """Undo what a file did to its feed's running total — e.g. a file sent by
    mistake. Only the latest file to touch those transactions can be withdrawn.
    The file and its output stay on record; this changes what the NEXT file is
    compared against."""
    with SessionLocal() as s:
        _need_tables(s)
        a = _arrival(s, arrival_id, principal)
        try:
            n = itd_store.withdraw(s, a.id)
        except itd_store.CannotWithdraw as e:
            raise HTTPException(409, str(e))
        if n:
            a.itd_summary = {**(a.itd_summary or {}), "status": "withdrawn"}
        s.commit()
        _log(_tenant_name(s, a.tenant_id) or "", _actor(principal), "itd_file_withdrawn",
             target=str(a.id), details={"filename": a.filename, "rows": n},
             principal=principal)
        return {"arrival_id": a.id, "rows_undone": n}


@router.get("/feeds/positions")
def positions(program_id: int = Query(...), broker_party_id: int = Query(default=0),
              status: Optional[str] = Query(default=None),
              limit: int = Query(default=200, ge=1, le=1000),
              offset: int = Query(default=0, ge=0),
              mga: Optional[str] = Query(default=None),
              principal: Principal = Depends(current_principal)):
    """A feed's running total: every transaction it has delivered, its latest
    money, and its status (active, dropped, removed, void)."""
    with SessionLocal() as s:
        _need_tables(s)
        tid = resolve_tenant_id(s, principal, mga)
        program_id, broker_id = itd_store.feed_of(program_id, broker_party_id)
        return itd_store.feed_positions(s, tid, program_id, broker_id, status=status,
                                        limit=limit, offset=offset)


class DecisionBody(BaseModel):
    decision: str            # removed | keep


@router.post("/positions/{position_id}/decision")
def decide(position_id: int, body: DecisionBody, mga: Optional[str] = Query(default=None),
           principal: Principal = Depends(require_role("carrier_admin"))):
    """Decide on a transaction an ITD stopped carrying: 'removed' (the broker
    took it out on purpose) or 'keep' (still live — left off by mistake)."""
    with SessionLocal() as s:
        _need_tables(s)
        tid = resolve_tenant_id(s, principal, mga)
        try:
            out = itd_store.decide_dropped(s, tid, position_id, body.decision,
                                           who=_actor(principal))
        except LookupError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        s.commit()
        _log(_tenant_name(s, tid) or "", _actor(principal), "itd_dropped_decided",
             target=f"position:{position_id}", details={"decision": body.decision},
             principal=principal)
        return out
