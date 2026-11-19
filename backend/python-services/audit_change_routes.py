"""The Audit Logs tabs added for tasks 48.1, 48.2 and 49.1:

  /audit/changes           Change History — every field changed, old → new, who, why
  /audit/rules             Rule History — every rule and mapping, version by version
  /audit/reinsurer-views   Downloads & Views — reinsurers on their treaty links
  /audit/record-choices    the contracts the "Contract / policy / file" filter offers

Every seat may call them; what differs is whose rows come back (audit_feed.
scope_for, the one visibility rule), exactly as for /audit/logs.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

import audit_record_filter as arf
import audit_reinsurer_views as arv
import change_history as chist
import rule_history as rhist
from audit_routes import _filters
from auth_deps import Principal, current_principal
from db import SessionLocal

router = APIRouter(prefix="/audit", tags=["audit"])


def _csv(rows: list[dict], columns, name: str) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow([label for _k, label in columns])
    for r in rows:
        w.writerow(["" if r.get(k) is None else r.get(k) for k, _label in columns])
    stamp = datetime.utcnow().strftime("%Y-%m-%d")
    # A BOM so Excel opens the UTF-8 file with its accents intact.
    return Response(content=("﻿" + buf.getvalue()).encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}-{stamp}.csv"'})


# ── Change History (48.1) ──────────────────────────────────────────────────
@router.get("/changes")
def change_list(since: Optional[str] = Query(default=None, alias="from"),
                until: Optional[str] = Query(default=None, alias="to"),
                actor: Optional[str] = None, q: Optional[str] = None,
                module: Optional[str] = None, entity: Optional[str] = None,
                page: int = 1, page_size: int = 50,
                principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        out = chist.changes(s, principal, _filters(None, since, until, actor, "all", q),
                            module=module or None, entity=entity or None, page=page, page_size=page_size)
        out["modules"] = list(chist.MODULES)
        return out


@router.get("/changes/download")
def change_download(since: Optional[str] = Query(default=None, alias="from"),
                    until: Optional[str] = Query(default=None, alias="to"),
                    actor: Optional[str] = None, q: Optional[str] = None,
                    module: Optional[str] = None, entity: Optional[str] = None,
                    principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        out = chist.changes(s, principal, _filters(None, since, until, actor, "all", q),
                            module=module or None, entity=entity or None, page=1, page_size=10**9)
    return _csv(out["items"], chist.CSV_COLUMNS, "change-history")


@router.get("/changes/{event_id}")
def change_detail(event_id: int, principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        return chist.change(s, principal, event_id)


# ── Rule History (48.2) ────────────────────────────────────────────────────
@router.get("/rules")
def rule_list(since: Optional[str] = Query(default=None, alias="from"),
              until: Optional[str] = Query(default=None, alias="to"),
              actor: Optional[str] = None, q: Optional[str] = None,
              programme: Optional[int] = None, kind: Optional[str] = None, status: Optional[str] = None,
              page: int = 1, page_size: int = 50,
              principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        return rhist.rules(s, principal, _filters(None, since, until, actor, "all", q),
                           programme=programme, kind=kind or None, status=status or None,
                           page=page, page_size=page_size)


@router.get("/rules/download")
def rule_download(since: Optional[str] = Query(default=None, alias="from"),
                  until: Optional[str] = Query(default=None, alias="to"),
                  actor: Optional[str] = None, q: Optional[str] = None,
                  programme: Optional[int] = None, kind: Optional[str] = None, status: Optional[str] = None,
                  principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        out = rhist.rules(s, principal, _filters(None, since, until, actor, "all", q),
                          programme=programme, kind=kind or None, status=status or None,
                          page=1, page_size=10**9)
    return _csv(out["items"], rhist.CSV_COLUMNS, "rule-history")


@router.get("/rules/versions")
def rule_versions(key: str, principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        return rhist.versions(s, principal, key)


# ── Downloads & Views: reinsurer links (49.1) ──────────────────────────────
@router.get("/reinsurer-views")
def reinsurer_views(since: Optional[str] = Query(default=None, alias="from"),
                    until: Optional[str] = Query(default=None, alias="to"),
                    q: Optional[str] = None, kind: str = "", mga: Optional[str] = None,
                    page: int = 1, page_size: int = 50,
                    principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        return arv.views(s, principal, _filters(None, since, until, None, "all", q),
                         kind=kind, mga=mga, page=page, page_size=page_size)


# ── the "Contract / policy / file" filter's contract list (49.1) ───────────
@router.get("/record-choices")
def record_choices(q: str = "", principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        return arf.choices(s, principal, q)
