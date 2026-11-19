"""
audit_reinsurer_views.py
────────────────────────
Task 49.1 — reinsurers who opened or downloaded a treaty through their private
link. Those people have no login, so they never reach access_log; their views
were kept only on the treaty's own page (reinsurer_access_event). This puts
them under Audit Logs → Downloads & Views as well, read-only, from that table.

WHO SEES WHAT
  Kavachio every carrier's (or one, by `mga`); a carrier its own treaties;
  a broker none — the treaty is between the carrier and its reinsurers.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

TYPES = {"viewed": "Viewed the treaty", "downloaded": "Downloaded a document"}


def _aware(x: Optional[dt.datetime]) -> Optional[dt.datetime]:
    """The audit filters hold naive UTC; this table's column is timezone-aware
    (and the dev database runs in Asia/Kolkata), so say it is UTC."""
    return x.replace(tzinfo=dt.timezone.utc) if x is not None and x.tzinfo is None else x


def views(s, principal, f, *, kind: str = "", mga: Optional[str] = None, page: int = 1,
          page_size: int = 50) -> dict:
    import audit_feed as af
    import audit_history as ah
    from db import Contract, Party, ReinsurerAccessEvent as E, ReinsurerAccessLink as L
    v = af.viewer_for(s, principal)
    empty = {"items": [], "total": 0, "page": 1, "page_size": page_size,
             "counts": {"viewed": 0, "downloaded": 0, "people": 0}}
    if v.is_broker:
        return {**empty, "hidden": True}
    q = s.query(E).filter(E.type.in_(list(TYPES)))
    if v.is_platform:
        if mga:
            from app_routes import resolve_tenant_id
            q = q.filter(E.tenant_id == resolve_tenant_id(s, principal, mga))
    else:
        if v.tenant_id is None:
            return empty
        q = q.filter(E.tenant_id == v.tenant_id)
    if f.since is not None:
        q = q.filter(E.at >= _aware(f.since))
    if f.until is not None:
        q = q.filter(E.at <= _aware(f.until))
    rows = q.order_by(E.at.desc(), E.id.desc()).limit(5000).all()
    links = {lk.id: lk for lk in s.query(L).filter(L.id.in_({r.link_id for r in rows} or {0}))}
    parties = dict(s.query(Party.id, Party.legal_name).filter(
        Party.id.in_({lk.party_id for lk in links.values()} or {0})))
    treaties = {c.id: (c.name or c.filename or f"Treaty #{c.id}") for c in
                s.query(Contract).filter(Contract.id.in_({r.contract_id for r in rows} or {0}))}
    items = []
    for r in rows:
        lk = links.get(r.link_id)
        d = r.detail if isinstance(r.detail, dict) else {}
        item = {"id": r.id, "at": ah._iso(r.at), "type": r.type,
                "status": "Downloaded" if r.type == "downloaded" else "Viewed",
                "what": TYPES.get(r.type, r.type), "person": (lk.name if lk else None) or r.actor or "—",
                "email": (lk.email if lk else None) or r.actor, "company": parties.get(lk.party_id) if lk else None,
                "treaty": treaties.get(r.contract_id) or f"Treaty #{r.contract_id}",
                "contract_id": r.contract_id, "document": d.get("filename") or d.get("document"),
                "ip": r.ip}
        if f.q and f.q.lower() not in " ".join(str(item.get(k) or "") for k in
                                              ("person", "email", "company", "treaty", "document")).lower():
            continue
        items.append(item)
    counts = {"viewed": sum(1 for i in items if i["type"] == "viewed"),
              "downloaded": sum(1 for i in items if i["type"] == "downloaded"),
              "people": len({i["email"] for i in items if i["email"]})}
    if kind in ("view", "download"):
        items = [i for i in items if i["type"] == ("viewed" if kind == "view" else "downloaded")]
    out = ah._page(items, page, page_size)
    out["counts"] = counts
    return out
