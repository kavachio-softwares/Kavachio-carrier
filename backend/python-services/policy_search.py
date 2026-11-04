"""
policy_search.py
────────────────
Policy Search (Phase 2, 43.1): the policies inside the processed bordereaux,
searched and filtered by programme, broker, insurer, state and dates.

WHERE THE POLICIES COME FROM
  The rows of every processed bordereau — read back from its rendered file
  through its output template, whose columns are each tied to a data-model
  field (ceded_engine.read_export_rows, the same reader the reinsurance
  statements use). The canonical `policy` table is not used: it is filled
  only for files whose format Kavachio has mapped into the data model, which
  is a small fraction of what has been processed.

  Only the CURRENT version of each file counts (exception_tally
  .current_export_ids — the same set every other screen counts): a file
  corrected and re-run replaces the earlier one here too, and a month a
  replacement contract has answered leaves the replaced contract's file out.

WHO SEES WHAT
  carrier   every policy in every current file of its own company, whichever
            broker sent it. Never another carrier's.
  broker    only the policies in the files sent as that broker, on the
            programmes it is still on (an ACTIVE program_broker link — the
            same rule as the broker's own file list), across every carrier
            that has it on one. Never another broker's.
  Kavachio  none: policy data belongs to the carrier and its broker.

ONE LINE PER POLICY
  A policy appears in many files — a new business month, then endorsements,
  then a cancellation. The search shows it once per carrier, programme,
  broker and policy number, carrying its latest values and the total of its
  premium; `detail` lists every bordereau row behind it.

Reading never writes. What a file yields is remembered in memory until the
file or its template changes, so paging and filtering reuse it.

Every function takes the caller's session; none opens one.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import func

import ceded_engine as ce
from rule_windows import as_date

# ── standard field names, beside ceded_engine's POLICY / INSURED / PREMIUM ──
# The state a policy is filed in: the home state when a template names it,
# else the risk location's.
STATE = ("policy_home_state", "policy_risk_subdivision",
         "risk_location_subdivision", "policyholder_subdivision")
COVER_START = ("policy_effective_date",)
COVER_END = ("policy_expiration_date",)

# Which date the date filter reads.
DATE_FIELDS = ("start", "end", "month")

# Rows remembered across all files; the least recently used files go first.
CACHE_ROWS = 400_000

_LOCK = threading.Lock()
_REMEMBERED: "OrderedDict[int, tuple[str, list[dict]]]" = OrderedDict()


# ── who is asking ───────────────────────────────────────────────────────────
@dataclass
class Seat:
    kind: str                                  # "carrier" | "broker"
    tenant_id: Optional[int] = None            # carrier seat
    broker_party_id: Optional[int] = None      # broker seat
    program_ids: set[int] = field(default_factory=set)   # broker: live links


def seat_of(s, p) -> Seat:
    """The carrier or broker this person searches for; refuses anyone else."""
    from carrier_scope import broker_party_id_of
    from db import ProgramBroker
    if p.is_broker:
        bid = broker_party_id_of(s, p)
        if bid is None:
            raise HTTPException(403, "no broker bound to this user")
        progs = {pid for (pid,) in s.query(ProgramBroker.program_id).filter(
            ProgramBroker.broker_party_id == bid,
            func.coalesce(ProgramBroker.status, "active") == "active")}
        return Seat("broker", broker_party_id=bid, program_ids=progs)
    if p.role == "carrier_admin" and p.tenant_id is not None:
        return Seat("carrier", tenant_id=int(p.tenant_id))
    raise HTTPException(403, "Policy search is for carriers and brokers.")


def _exports(s, seat: Seat, carrier_id: Optional[int] = None) -> list:
    """The current processed files this seat may read, oldest first."""
    from db import OutputExport
    from exception_tally import current_export_ids
    q = s.query(OutputExport.id, OutputExport.tenant_id, OutputExport.program_id,
                OutputExport.broker_party_id, OutputExport.contract_id,
                OutputExport.template_id, OutputExport.filename,
                OutputExport.blob_ref, OutputExport.created_at,
                func.md5(OutputExport.blob).label("blob_md5"))
    if seat.kind == "carrier":
        live = current_export_ids(s, tenant_id=seat.tenant_id)
        q = q.filter(OutputExport.tenant_id == seat.tenant_id)
    else:
        if not seat.program_ids:
            return []
        live = current_export_ids(s, broker_party_id=seat.broker_party_id)
        q = q.filter(OutputExport.broker_party_id == seat.broker_party_id,
                     OutputExport.program_id.in_(seat.program_ids))
        if carrier_id is not None:
            q = q.filter(OutputExport.tenant_id == carrier_id)
    if not live:
        return []
    return q.filter(OutputExport.id.in_(live)).order_by(OutputExport.id).all()


# ── reading one file ────────────────────────────────────────────────────────
def _plain(v: Any) -> Any:
    """A cell as JSON can carry it: dates as ISO dates, numbers as numbers."""
    if isinstance(v, dt.datetime):
        return v.date().isoformat() if v.time() == dt.time() else v.isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    return str(v)


def _text(v: Any) -> Optional[str]:
    if v is None:
        return None
    t = " ".join(str(_plain(v)).split())
    return t or None


def policy_key(number: Any) -> str:
    """Two spellings of one policy number group together."""
    return " ".join(str(number).split()).upper()


def _template_stamp(structure: dict) -> str:
    """What in a template decides which column is which field."""
    shape = [(sh.get("sheet_name"), sh.get("data_start_row"),
              [(c.get("column_index"), c.get("canonical_field"), c.get("column_name"))
               for c in sh.get("columns") or []])
             for sh in (structure or {}).get("sheets") or []]
    return hashlib.sha256(json.dumps(shape, default=str).encode()).hexdigest()[:16]


def _day(v: Any) -> Optional[str]:
    """A cover date as a calendar day; text the file wrote any other way is
    kept as written."""
    d = as_date(v)
    return d.isoformat() if d else _text(v)


def _state(r: dict) -> Optional[str]:
    """The first state field that holds a state. A date there is a column
    mapped wrongly in the setup, not a state, and is passed over."""
    for k in STATE:
        v = r.get(k)
        if v not in (None, "") and not isinstance(v, dt.date):
            return _text(v)
    return None


def _line(r: dict, index: int) -> Optional[dict]:
    """One bordereau row, reduced to what the search shows; None when the row
    has no policy number (a total or a note, not a policy)."""
    number = _text(ce.first(r, ce.POLICY))
    if number is None:
        return None
    return {
        "row": index,
        "policy": number,
        "insured": _text(ce.first(r, ce.INSURED)),
        "state": _state(r),
        "start": _day(ce.first(r, COVER_START)),
        "end": _day(ce.first(r, COVER_END)),
        "transaction": _text(ce.first(r, ce.TXN)),
        "premium": ce.num(ce.first(r, ce.PREMIUM)),
        "currency": _text(ce.first(r, ce.CURRENCY)),
    }


def _size() -> int:
    return sum(len(lines) for _stamp, lines in _REMEMBERED.values())


def _recall(export_id: int, stamp: str) -> Optional[list[dict]]:
    with _LOCK:
        hit = _REMEMBERED.get(export_id)
        if hit and hit[0] == stamp:
            _REMEMBERED.move_to_end(export_id)
            return hit[1]
    return None


def _remember(export_id: int, stamp: str, lines: list[dict]) -> None:
    with _LOCK:
        _REMEMBERED[export_id] = (stamp, lines)
        _REMEMBERED.move_to_end(export_id)
        while len(_REMEMBERED) > 1 and _size() > CACHE_ROWS:
            _REMEMBERED.popitem(last=False)


def _raw_rows(s, o, structure: dict) -> list[dict]:
    import storage
    from db import OutputExport
    blob = None if o.blob_ref else s.query(OutputExport.blob).filter(
        OutputExport.id == o.id).scalar()
    data = storage.resolve_bytes(o.blob_ref, blob)
    rows, _notes = ce.read_export_rows(structure, data, o.filename or "")
    return rows


def _lines_of(s, o, structure: Optional[dict]) -> list[dict]:
    """The policy rows of one processed file, remembered until it changes."""
    if structure is None:
        return []
    stamp = f"{o.template_id}:{_template_stamp(structure)}:{o.blob_ref or o.blob_md5}"
    hit = _recall(o.id, stamp)
    if hit is not None:
        return hit
    lines = [ln for i, r in enumerate(_raw_rows(s, o, structure), start=1)
             if (ln := _line(r, i)) is not None]
    _remember(o.id, stamp, lines)
    return lines


def _structures(s, template_ids) -> dict[int, dict]:
    from sqlalchemy.orm import load_only
    from db import ExportTemplate
    ids = {i for i in template_ids if i}
    if not ids:
        return {}
    return {t.id: (t.structure or {}) for t in s.query(ExportTemplate)
            .options(load_only(ExportTemplate.structure))
            .filter(ExportTemplate.id.in_(ids))}


# ── names ───────────────────────────────────────────────────────────────────
def _names(s, exports) -> dict:
    from db import Party, Program, Tenant
    progs = {o.program_id for o in exports if o.program_id}
    brokers = {o.broker_party_id for o in exports if o.broker_party_id}
    tenants = {o.tenant_id for o in exports if o.tenant_id}
    return {
        "programme": dict(s.query(Program.id, Program.name)
                          .filter(Program.id.in_(progs)).all()) if progs else {},
        "broker": dict(s.query(Party.id, Party.legal_name)
                       .filter(Party.id.in_(brokers)).all()) if brokers else {},
        "insurer": {i: (legal or code) for i, legal, code in
                    s.query(Tenant.id, Tenant.legal_name, Tenant.tenant_name)
                    .filter(Tenant.id.in_(tenants)).all()} if tenants else {},
    }


def _period(o, versions: dict) -> Optional[str]:
    """The month a file reports on, from its submission (exception_tally
    .export_versions); None for a run that is part of no submission."""
    return (versions.get(o.id) or {}).get("reporting_period")


# ── the search ──────────────────────────────────────────────────────────────
def _policies(s, seat: Seat, carrier_id: Optional[int]) -> tuple[list[dict], dict, int]:
    """Every policy this seat may read, one per carrier + programme + broker +
    policy number. Returns (policies, names, files read)."""
    from exception_tally import export_versions
    exports = _exports(s, seat, carrier_id)
    if not exports:
        return [], {"programme": {}, "broker": {}, "insurer": {}}, 0
    structures = _structures(s, {o.template_id for o in exports})
    versions = export_versions(s, [o.id for o in exports])
    names = _names(s, exports)
    groups: dict[tuple, dict] = {}
    for o in exports:
        period = _period(o, versions)
        for ln in _lines_of(s, o, structures.get(o.template_id)):
            key = (o.tenant_id, o.program_id, o.broker_party_id, policy_key(ln["policy"]))
            g = groups.get(key)
            if g is None:
                g = groups[key] = {
                    "insurer_id": o.tenant_id, "program_id": o.program_id,
                    "broker_party_id": o.broker_party_id,
                    "programme": names["programme"].get(o.program_id),
                    "broker": names["broker"].get(o.broker_party_id),
                    "insurer": names["insurer"].get(o.tenant_id),
                    "rows": 0, "files": set(), "premium": 0.0, "has_premium": False,
                    "currencies": set(), "first_period": period, "last_period": period,
                    "_at": (), "insured": None, "state": None, "start": None, "end": None,
                    "transaction": None, "policy": ln["policy"],
                }
            g["rows"] += 1
            g["files"].add(o.id)
            if ln["premium"] is not None:
                g["premium"] += ln["premium"]
                g["has_premium"] = True
            if ln["currency"]:
                g["currencies"].add(ln["currency"])
            if period and (g["first_period"] is None or period < g["first_period"]):
                g["first_period"] = period
            # The latest row speaks for the policy; an earlier row fills in
            # only what the latest left blank.
            at = (period or "", o.id, ln["row"])
            latest = at >= g["_at"]
            if latest:
                g["_at"], g["last_period"], g["policy"] = at, period, ln["policy"]
            for k in ("insured", "state", "start", "end", "transaction"):
                if ln[k] is not None and (latest or g[k] is None):
                    g[k] = ln[k]
    out = []
    for g in groups.values():
        cur = g.pop("currencies")
        g["files"] = len(g["files"])
        g["currency"] = next(iter(cur)) if len(cur) == 1 else None
        # Amounts in different currencies are not added up.
        g["premium"] = round(g["premium"], 2) if g.pop("has_premium") and len(cur) <= 1 else None
        g.pop("_at")
        out.append(g)
    return out, names, len(exports)


def _state_choices(items: list[dict]) -> list[str]:
    """Each state once: "tx" and "TX" are one choice (the filter ignores case),
    shown in capitals when any file wrote it so."""
    seen: dict[str, str] = {}
    for g in items:
        v = g["state"]
        if v and (v.lower() not in seen or v.isupper()):
            seen[v.lower()] = v
    return sorted(seen.values(), key=str.lower)


def _in_range(value: Any, lo: Optional[dt.date], hi: Optional[dt.date]) -> bool:
    d = as_date(value)
    if d is None:
        return False
    return (lo is None or d >= lo) and (hi is None or d <= hi)


def _month_in_range(period: Optional[str], lo: Optional[dt.date],
                    hi: Optional[dt.date]) -> bool:
    b = ce.period_bounds(period or "")
    if not b:
        return False
    return (lo is None or b[1] >= lo) and (hi is None or b[0] <= hi)


def search(s, p, *, q: Optional[str] = None, program_id: Optional[int] = None,
           broker_party_id: Optional[int] = None, carrier_id: Optional[int] = None,
           state: Optional[str] = None, date_field: str = "start",
           date_from: Optional[str] = None, date_to: Optional[str] = None,
           page: int = 1, page_size: int = 25) -> dict:
    """One page of this seat's policies, and the choices its filters offer.

    `carrier_id` narrows a BROKER to one carrier (the sidebar's carrier
    switch); a carrier seat is always its own company and ignores it.
    Every filter is applied before the page is cut, so `total` counts the
    matches, not the page."""
    if date_field not in DATE_FIELDS:
        raise HTTPException(400, f"date_field must be one of {', '.join(DATE_FIELDS)}")
    lo, hi = as_date(date_from), as_date(date_to)
    if (date_from and lo is None) or (date_to and hi is None):
        raise HTTPException(400, "dates must be YYYY-MM-DD")
    seat = seat_of(s, p)
    items, names, files = _policies(s, seat, carrier_id if seat.kind == "broker" else None)

    # The filters' choices come from the whole set, so picking one never
    # empties the other lists.
    def _choices(key: str, label: str) -> list[dict]:
        seen = {(g[key], g[label]) for g in items if g[key] is not None}
        return sorted(({"id": i, "name": n or f"#{i}"} for i, n in seen),
                      key=lambda c: c["name"].lower())
    filters = {
        "programmes": _choices("program_id", "programme"),
        "brokers": _choices("broker_party_id", "broker") if seat.kind == "carrier" else [],
        "insurers": _choices("insurer_id", "insurer") if seat.kind == "broker" else [],
        "states": _state_choices(items),
    }

    needle = " ".join((q or "").split()).lower()
    want_state = (state or "").strip().lower()

    def keep(g: dict) -> bool:
        if program_id is not None and g["program_id"] != program_id:
            return False
        if seat.kind == "carrier" and broker_party_id is not None \
                and g["broker_party_id"] != broker_party_id:
            return False
        if want_state and (g["state"] or "").lower() != want_state:
            return False
        if needle and needle not in (g["policy"] or "").lower() \
                and needle not in (g["insured"] or "").lower():
            return False
        if lo is not None or hi is not None:
            if date_field == "month":
                return _month_in_range(g["last_period"], lo, hi)
            return _in_range(g["start" if date_field == "start" else "end"], lo, hi)
        return True

    hits = [g for g in items if keep(g)]
    # Latest bordereau month first; within a month, by policy number.
    hits.sort(key=lambda g: (g["policy"] or "").lower())
    hits.sort(key=lambda g: g["last_period"] or "", reverse=True)
    start = (page - 1) * page_size
    return {
        "items": hits[start:start + page_size],
        "total": len(hits),
        "page": page,
        "page_size": page_size,
        "seat": seat.kind,
        "files": files,
        "filters": filters,
    }


def detail(s, p, *, insurer_id: Optional[int], program_id: Optional[int],
           broker_party_id: Optional[int], policy: str,
           carrier_id: Optional[int] = None) -> dict:
    """Every bordereau row behind one policy of the search, file by file, with
    each row's full set of columns. 404 when this seat cannot see it."""
    from db import Contract
    from exception_tally import export_versions
    seat = seat_of(s, p)
    want = policy_key(policy)
    exports = [o for o in _exports(s, seat, carrier_id if seat.kind == "broker" else None)
               if o.tenant_id == insurer_id and o.program_id == program_id
               and o.broker_party_id == broker_party_id]
    structures = _structures(s, {o.template_id for o in exports})
    holding = [o for o in exports
               if any(policy_key(ln["policy"]) == want
                      for ln in _lines_of(s, o, structures.get(o.template_id)))]
    if not holding:
        raise HTTPException(404, "policy not found")
    versions = export_versions(s, [o.id for o in holding])
    names = _names(s, holding)
    contracts = {c.id: (c.name or c.filename) for c in s.query(Contract).filter(
        Contract.id.in_({o.contract_id for o in holding if o.contract_id} or {0}))}
    rows = []
    for o in holding:
        structure = structures.get(o.template_id) or {}
        v = versions.get(o.id) or {}
        for i, r in enumerate(_raw_rows(s, o, structure), start=1):
            ln = _line(r, i)
            if ln is None or policy_key(ln["policy"]) != want:
                continue
            rows.append({
                **ln,
                "export_id": o.id,
                "filename": o.filename,
                "period": _period(o, versions),
                "version_no": v.get("version_no"),
                "contract_id": o.contract_id,
                "contract": contracts.get(o.contract_id),
                "processed_at": o.created_at.isoformat() + "Z" if o.created_at else None,
                "columns": [{"name": k, "value": _plain(val)}
                            for k, val in (r.get("_col") or {}).items()],
            })
    rows.sort(key=lambda r: (r["period"] or "", r["export_id"], r["row"]), reverse=True)
    first = holding[0]
    return {
        "policy": rows[0]["policy"] if rows else policy,
        "insurer_id": insurer_id,
        "program_id": program_id,
        "broker_party_id": broker_party_id,
        "insurer": names["insurer"].get(first.tenant_id),
        "programme": names["programme"].get(first.program_id),
        "broker": names["broker"].get(first.broker_party_id),
        "seat": seat.kind,
        "rows": rows,
    }
