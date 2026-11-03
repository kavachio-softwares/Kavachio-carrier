"""Is a contract set up to be checked — and, when it is not, how it gets there.

A bordereau is checked against its CONTRACT's checks (validation_rule rows),
written for the output template of the Bordereau Setup that runs it. Two gaps
let a file through with none:

  · Process Bordereau opened for ANY live setup on the programme and broker
    (setup_scope.live_setup_for falls back to the newest one), whether or not
    the chosen contract had a single check for it. A contract uploaded to
    replace another (prg test: 8204 replacing 7520) was never set up, and its
    files ran on the old contract's setup with no contract checks at all;
  · such a run was stored as "clean" — only the date and amount formats had
    been looked at (validation_outcome).

This module is the one answer to "does this contract have checks for the setup
that would run it", asked by the broker's readiness check, every run
(direct_routes._prepare_run, so the email / SFTP / API intake too), the
contract record and the mid-term change. And it does the one thing that sets a
contract up on a setup that already exists: make its checks for that setup's
output template from its clauses (or typed terms) — the step Bordereau Setup
itself runs — and link it to the setup, taking the place of the contract it
replaced.

Counting follows the run exactly: the setup's sheet pins (moved along a
renewal chain to the named contract, renewal_chain.follow_pins), then the
named contract — or the setup's own fallback contract when none is named —
active rules only, filtered by the setup's rule choice (rule_scope).
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import text

log = logging.getLogger(__name__)

CODE = "contract_not_set_up"

# The opening words of what a held intake file says. intake_autorun finds the
# files waiting for a contract's checks by them, as it finds those waiting for
# a setup; "setup" is in it so submission_service files it as on hold.
HELD_PREFIX = "The contract this file is for has no checks yet."


def held_error(contract_name: str) -> str:
    return (f"{HELD_PREFIX} “{contract_name}” has not been set up in Bordereau "
            f"Setup, so there is nothing to check the file against. The file is "
            f"kept and is processed automatically as soon as its checks are made.")


def contract_name(c) -> str:
    return (getattr(c, "name", None) or getattr(c, "filename", None)
            or f"Contract {getattr(c, 'id', '')}")


def message(name: str, *, for_broker: Optional[bool] = None) -> str:
    """Why a file can't be processed under a contract with no checks — said to
    the broker, to the carrier, or (None) to whoever ran it."""
    if for_broker is None:
        return (f"“{name}” has no checks for this Bordereau Setup yet, so the file "
                f"can't be checked against the contract. The carrier sets the "
                f"contract up in Bordereau Setup — with the existing setup's "
                f"templates or a new setup — and then it can be processed.")
    if for_broker:
        return (f"Your carrier has not set up the checks for “{name}” yet, so a "
                f"file sent under it can't be checked against the contract. Ask "
                f"them to finish its Bordereau Setup — uploads open as soon as "
                f"they do.")
    return (f"“{name}” has no checks for this Bordereau Setup yet, so a file can't "
            f"be checked against the contract. Set the contract up first: use the "
            f"existing setup's templates to make its checks, or build a new setup.")


# ── what a run would check ───────────────────────────────────────────────────

def governing_ids(s, pipe, contract_id: Optional[int]) -> list[int]:
    """The contracts whose checks a run on `pipe` naming `contract_id` uses —
    the same set direct_routes._render_landing builds (before the as-of swap,
    which only exchanges a pin for a version of the same contract)."""
    from db import PipelineContract
    rows = (s.query(PipelineContract)
            .filter(PipelineContract.pipeline_id == pipe.id)
            .order_by(PipelineContract.position, PipelineContract.id).all())
    sheet = {r.sheet_key: r.contract_id for r in rows if r.sheet_key}
    fallback = next((r.contract_id for r in rows if not r.sheet_key), None)
    if contract_id and sheet:
        import renewal_chain as rc
        if rc.routing_enabled():
            sheet = rc.follow_pins(sheet, int(contract_id), rc.chain_ids(s, int(contract_id)))
    out: list[int] = []
    for cid in [int(v) for v in sheet.values() if v] + (
            [int(contract_id or fallback)] if (contract_id or fallback) else []):
        if cid not in out:
            out.append(cid)
    return out


def check_count(s, pipe, contract_id: Optional[int]) -> int:
    """How many contract checks a run on `pipe` naming `contract_id` would run."""
    import rule_scope
    ids = governing_ids(s, pipe, contract_id)
    if not ids:
        return 0
    rows = s.execute(text(
        f"SELECT rule_id, contract_id, {rule_scope.tag_select(s)} "
        "FROM validation_rule WHERE contract_id = ANY(:ids) AND rule_status = 'active'"),
        {"ids": ids}).mappings().all()
    if not rows:
        return 0
    return len(rule_scope.filter_rules(
        s, [dict(r) for r in rows], setup_template_id=pipe.output_template_id,
        scope=rule_scope.pipeline_scope(s, pipe.id)))


# ── the contract's side ─────────────────────────────────────────────────────

def _live_setups(s, c) -> list:
    """Live setups a bordereau under this contract could run on: the broker's
    own first, then the programme-wide one, newest first."""
    from db import Pipeline
    q = (s.query(Pipeline)
         .filter(Pipeline.tenant_id == c.tenant_id,
                 Pipeline.program_id == c.program_id,
                 Pipeline.status == "active"))
    own = (q.filter(Pipeline.broker_party_id == c.broker_party_id)
           .order_by(Pipeline.id.desc()).all() if c.broker_party_id else [])
    wide = q.filter(Pipeline.broker_party_id.is_(None)).order_by(Pipeline.id.desc()).all()
    return own + wide


def serving_setup(s, c):
    """The live setup a run under this contract uses (setup_scope), or None."""
    setups = _live_setups(s, c)
    if not setups:
        return None
    from setup_scope import live_setup_for
    return live_setup_for(s, c.tenant_id, setups[0].carrier_party_id, c.program_id,
                          c.broker_party_id, c.id)


RUNNABLE = ("active", "expired")


def state(s, c, lifecycle: str) -> Optional[dict]:
    """What the contract record says about this contract's checks, for the
    carrier. None while the contract is not one files are sent under."""
    if lifecycle not in RUNNABLE or not c.program_id:
        return None
    pipe = serving_setup(s, c)
    new_setup = (f"/direct/setup?program_id={c.program_id}"
                 + (f"&broker_party_id={c.broker_party_id}" if c.broker_party_id else ""))
    if pipe is None:
        return {"state": "no_setup", "checks": 0, "setup": None, "new_setup_link": new_setup}
    from db import ExportTemplate
    from setup_scope import contract_ids_of
    tpl = s.get(ExportTemplate, pipe.output_template_id) if pipe.output_template_id else None
    n = check_count(s, pipe, c.id)
    return {
        "state": "ready" if n else "needs_checks",
        "checks": n,
        "dates": _dates(s, c, pipe, tpl),
        "setup": {"id": pipe.id, "name": pipe.name or f"Setup {pipe.id}",
                  "broker_party_id": pipe.broker_party_id,
                  "output_template": ({"id": tpl.id, "name": tpl.name} if tpl else None),
                  "lists_contract": c.id in contract_ids_of(s, pipe.id)},
        "new_setup_link": new_setup,
        "edit_link": f"/direct/setups/{pipe.id}/edit",
    }


def _dates(s, c, pipe, tpl) -> Optional[dict]:
    """For a contract whose checks change on a date (a mid-term change): which
    column of the setup's output dates each row. A dated check can't place a
    row without one, and leaves it unchecked instead — so when neither the
    setup nor the template's tags name it, the carrier picks (the same choice
    the mid-term change page asks for). None when nothing is dated."""
    dated = s.execute(text(
        "SELECT COUNT(*) FROM validation_rule WHERE contract_id = :c AND rule_status = 'active' "
        "AND (source_amendment_id IS NOT NULL OR effective_to IS NOT NULL)"),
        {"c": c.id}).scalar() or 0
    if not dated:
        return None
    import rule_windows as rw
    from direct_routes import _load_structure
    structure = _load_structure(tpl) if tpl is not None else {"sheets": []}
    cols: list[str] = []
    for sh in structure.get("sheets") or []:
        for col in sh.get("columns") or []:
            name = col.get("column_name")
            if name and name not in cols:
                cols.append(name)
    saved = (pipe.governing_date_field or "").strip() or None
    return {"dated_checks": int(dated), "column": saved or rw.date_column_from_structure(structure),
            "saved": saved, "columns": cols}


def _replaces(s, old, new) -> bool:
    """Is `old` the contract `new` took the place of on this programme and
    broker? A superseded contract that is not part of new's renewal chain (a
    renewal reaches its setup through renewal_chain.follow_pins and the
    template chain, and keeps its own late months) and has the same schedule."""
    if old is None or old.id == new.id:
        return False
    import contract_routes as cr
    if cr._effective_lifecycle(old) != "superseded" and (old.status or "") != "superseded":
        return False
    if old.program_id != new.program_id or old.broker_party_id != new.broker_party_id:
        return False
    if (old.schedule_key or None) != (new.schedule_key or None):
        return False
    import renewal_chain as rc
    return old.id not in rc.chain_ids(s, new.id)


def link(s, pipe, c) -> dict:
    """Put the contract on the setup: in the place of the contract it replaced
    (every row — the fallback and any sheet pin), else as one more contract the
    setup serves. Nothing changes for a contract the setup already lists, or
    one that reaches it along its renewal chain. The caller commits."""
    from db import Contract, PipelineContract
    from setup_scope import contract_ids_of
    rows = (s.query(PipelineContract)
            .filter(PipelineContract.pipeline_id == pipe.id)
            .order_by(PipelineContract.position, PipelineContract.id).all())
    if c.id in {r.contract_id for r in rows}:
        return {"linked": "already", "replaced": []}
    import renewal_chain as rc
    chain = set(rc.chain_ids(s, c.id))
    if rows and any(r.contract_id in chain for r in rows):
        return {"linked": "renewal_chain", "replaced": []}
    replaced = []
    for r in rows:
        if _replaces(s, s.get(Contract, r.contract_id), c):
            replaced.append(r.contract_id)
            r.contract_id = c.id
    if replaced:
        return {"linked": "replaced", "replaced": sorted(set(replaced))}
    if not rows and not contract_ids_of(s, pipe.id):
        # A setup listing no contract serves every contract already.
        return {"linked": "serves_all", "replaced": []}
    s.add(PipelineContract(tenant_id=pipe.tenant_id, pipeline_id=pipe.id,
                           contract_id=c.id, sheet_key=None,
                           position=max([r.position or 0 for r in rows] or [0]) + 1))
    return {"linked": "added", "replaced": []}
