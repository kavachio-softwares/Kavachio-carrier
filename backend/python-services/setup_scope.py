"""Which live Bordereau Setup serves a contract.

A setup is built for (carrier, programme, broker) and for the contract(s) it
lists, and it writes into ONE output template. A broker holding two contracts
with two different BDX templates therefore needs two setups, both live at once
— so "the broker's newest live setup" no longer answers "which setup runs this
contract". This does, and every reader asks it here: Process Bordereau, the
broker's readiness check, the template lookup and the template download, so no
screen can name one setup while the run uses another.

Most specific first:
  1. a setup that LISTS the contract and writes into the contract's template
  2. a setup that writes into the contract's template
  3. a setup that lists the contract
  4. the newest live setup — what every reader did before this, kept so a
     contract with nothing of its own still reaches the refusal saying why
Within each rung the broker's own setups come before the programme-wide one,
newest first. With no contract named the answer is exactly what it always was:
the broker's newest live setup, else the programme's.
"""
from __future__ import annotations

from typing import Any, Optional

_UNSET: Any = object()


def contract_ids_of(s, pipeline_id: int) -> set[int]:
    """The contracts a setup lists. Empty for a setup made before contracts were
    attached — which covers every contract, as it always has."""
    from db import PipelineContract
    return {cid for (cid,) in s.query(PipelineContract.contract_id)
            .filter(PipelineContract.pipeline_id == pipeline_id).all()}


def same_template(s, running_id: Optional[int], agreed) -> bool:
    """Does a setup write into the agreed template? Versions of one template
    share a name, and a setup keeps the version it was built with — the same
    rule the run's own conflict check applies."""
    from db import ExportTemplate
    if agreed is None or not running_id:
        return False
    if running_id == agreed.id:
        return True
    row = s.get(ExportTemplate, running_id)
    return bool(row and row.name == agreed.name)


def _live(s, tid: int, carrier_party_id: Optional[int], program_id: int,
          broker: Optional[int]) -> list:
    from db import Pipeline
    q = (s.query(Pipeline)
         .filter(Pipeline.tenant_id == tid,
                 Pipeline.carrier_party_id == carrier_party_id,
                 Pipeline.program_id == program_id,
                 Pipeline.status == "active"))
    q = q.filter(Pipeline.broker_party_id == broker if broker is not None
                 else Pipeline.broker_party_id.is_(None))
    return q.order_by(Pipeline.id.desc()).all()


def live_setup_for(s, tid: int, carrier_party_id: Optional[int], program_id: int,
                   broker_party_id: Optional[int], contract_id: Optional[int],
                   agreed: Any = _UNSET):
    """The live setup a bordereau for this scope runs on, or None.

    `agreed` is the contract's output template when the caller already has it;
    left out, it is resolved here."""
    tiers = ([_live(s, tid, carrier_party_id, program_id, broker_party_id)]
             if broker_party_id else [])
    tiers.append(_live(s, tid, carrier_party_id, program_id, None))
    candidates = [p for tier in tiers for p in tier]
    if not candidates:
        return None
    if not contract_id:
        return candidates[0]
    if agreed is _UNSET:
        from output_template_routes import _resolve
        agreed, _ = _resolve(s, tid, carrier_party_id, program_id,
                             broker_party_id, contract_id)
    lists = {p.id: contract_id in contract_ids_of(s, p.id) for p in candidates}
    fits = {p.id: same_template(s, p.output_template_id, agreed) for p in candidates}
    for rung in (lambda p: lists[p.id] and fits[p.id],
                 lambda p: fits[p.id],
                 lambda p: lists[p.id]):
        for p in candidates:
            if rung(p):
                return p
    return candidates[0]
