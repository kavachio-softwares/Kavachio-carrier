"""Make an earlier Bordereau Setup active again (task 51.1).

Kavachio has no undo for a processed file. What can be put back is a setup: a
superseded Bordereau Setup made active again, replacing the one in use. It is
the same step as Activate (direct_routes._go_live) — made deliberate: the
carrier is told beforehand which setup it replaces, must say why, and the step
is recorded with the reason, who and when and what it replaced
("setup_made_active_again"; listed on Audit Logs → Versions & Restores).
Files already processed are not changed.

  GET  /pipelines/{id}/restore   what it would replace (the popup)
  POST /pipelines/{id}/restore   do it — {reason}
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth_deps import Principal
from direct_routes import assert_tenant_owns
from carrier_scope import require_carrier_admin
from db import ActivityEvent, Party, Pipeline, Program, SessionLocal

router = APIRouter(tags=["bordereau-setup"])

ACTION = "setup_made_active_again"


class RestoreIn(BaseModel):
    reason: str = ""


def _name(p: Pipeline) -> str:
    return p.name or f"Setup #{p.id}"


def _superseded(s, principal: Principal, pipeline_id: int) -> Pipeline:
    p = s.get(Pipeline, pipeline_id)
    if not p:
        raise HTTPException(404, "That setup was not found.")
    assert_tenant_owns(principal, p.tenant_id)
    if (p.status or "").strip().lower() != "superseded":
        raise HTTPException(409, "Only a setup that was replaced can be made active again.")
    return p


@router.get("/pipelines/{pipeline_id}/restore")
def restore_preview(pipeline_id: int, principal: Principal = Depends(require_carrier_admin("make a setup active again"))):
    import direct_routes as dr
    with SessionLocal() as s:
        p = _superseded(s, principal, pipeline_id)
        ready, reason = dr._pipeline_ready(s, p)
        return {"setup": _name(p), "ready": ready, "not_ready_reason": None if ready else reason,
                "replaces": [{"id": x.id, "name": _name(x)} for x in dr.replaced_by(s, p)]}


@router.post("/pipelines/{pipeline_id}/restore")
def restore(pipeline_id: int, body: RestoreIn,
            principal: Principal = Depends(require_carrier_admin("make a setup active again"))):
    import direct_routes as dr
    from audit import actor_columns
    from app_routes import _actor
    why = (body.reason or "").strip()
    if not why:
        raise HTTPException(400, {"message": "Say why you are doing this.",
                                  "errors": {"reason": "Why are you doing this?"}})
    with SessionLocal() as s:
        p = _superseded(s, principal, pipeline_id)
        replaced = [{"id": x.id, "name": _name(x)} for x in dr.replaced_by(s, p)]
        prog = s.get(Program, p.program_id) if p.program_id else None
        broker = s.get(Party, p.broker_party_id) if p.broker_party_id else None
        s.add(ActivityEvent(
            tenant_id=p.tenant_id, actor=_actor(principal), action=ACTION, target=f"pipeline:{p.id}",
            details={"pipeline_id": p.id, "name": _name(p), "reason": why[:1000], "replaced": replaced,
                     "program_id": p.program_id, "programme": prog.name if prog else None,
                     "broker_party_id": p.broker_party_id, "broker": broker.legal_name if broker else None},
            **actor_columns(principal)))
        result, _became, _links = dr._go_live(s, p, principal)     # commits, with the event
        tenant_id, format_id = p.tenant_id, p.input_format_id
    dr._after_go_live(result, tenant_id, format_id, principal)
    return {**result, "replaced": replaced}
