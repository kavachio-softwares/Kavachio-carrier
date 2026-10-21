"""
Reinsurance (ceded) bordereaux — the carrier's side of a treaty, every period.

    prepare   gather the live inward bordereaux for one treaty and one period,
              cede them (ceded_engine) and split them by reinsurer
    review    the lines, each reinsurer's totals, and what could not be ceded
    send      email every reinsurer its own statement (Excel), and put it on
              its private treaty link
    answer    the reinsurer agrees its statement or raises a query, on the link

PREPARED AUTOMATICALLY, SENT ON PURPOSE. Opening the list prepares a draft for
every period whose inward bordereaux are in and that has none yet, and a new
draft version when an inward file was corrected after sending. Nothing goes to
a reinsurer until the carrier admin presses Send.

A statement is due DUE_DAYS after its period ends; past that and unsent, it is
overdue.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from typing import Any, Optional

from fastapi import (APIRouter, Depends, File, Form, Header, HTTPException, Query,
                     Request, UploadFile)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, or_

import ceded_engine as ce
import ceded_layout as cl
import treaty_rules as trr
import treaty_types as tt
from app_routes import _iso_utc, resolve_tenant_id
from auth_deps import Principal, current_principal
from db import (
    AppUser, CededBordereau, CededDecision, CededInput, CededSetup, CededStatement, Contract, ExportTemplate,
    OutputExport,
    Party, PartyContact, Program, SessionLocal, Tenant,
)

log = logging.getLogger("kavachio.ceded")

router = APIRouter(tags=["reinsurance bordereaux"])
public_router = APIRouter(prefix="/treaty-view", tags=["reinsurance (public)"])

DUE_DAYS = 30
OPEN = ("draft",)
ANSWERED = ("agreed", "queried")


def _tr():
    import treaty_routes
    return treaty_routes


def _cr():
    import contract_routes
    return contract_routes


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(v) -> Optional[str]:
    if v is None:
        return None
    v = v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)
    return v.isoformat().replace("+00:00", "Z")


def due_date(period: str, days: Optional[int] = None) -> Optional[dt.date]:
    b = ce.period_bounds(period)
    return b[1] + dt.timedelta(days=DUE_DAYS if days is None else days) if b else None


# ── the setup ───────────────────────────────────────────────────────────────
def setup_of(s, c: Contract) -> Optional[CededSetup]:
    return s.query(CededSetup).filter(CededSetup.contract_id == c.id).first()


def active_setup(s, c: Contract) -> Optional[CededSetup]:
    su = setup_of(s, c)
    return su if su is not None and su.status == "active" else None


def _frequency(su: Optional[CededSetup]) -> str:
    return (su.frequency if su is not None else None) or "monthly"



# =============================================================================
#  Gathering the inward side
# =============================================================================

def _protected_ids(s, c: Contract) -> list[int]:
    """The programmes feeding this treaty's statements: those it protects,
    narrowed to the setup's choice when it made one."""
    ids = [p["id"] for p in _tr().programmes(s, c)]
    su = setup_of(s, c)
    if su is not None and su.program_ids:
        keep = {int(x) for x in su.program_ids}
        ids = [i for i in ids if i in keep]
    return ids


def _live_exports(s, tid: int, program_ids: list[int]) -> list[tuple[OutputExport, dict]]:
    """Every live (latest-version) inward bordereau on these programmes, with
    its version info (reporting_period, version_no)."""
    from exception_tally import current_export_ids, export_versions
    if not program_ids:
        return []
    live = current_export_ids(s, tenant_id=tid)
    if not live:
        return []
    rows = (s.query(OutputExport)
            .filter(OutputExport.id.in_(live),
                    OutputExport.program_id.in_(program_ids))
            .order_by(OutputExport.id).all())
    vers = export_versions(s, [o.id for o in rows])
    return [(o, vers.get(o.id) or {}) for o in rows]


def rules_for(s, c: Contract, st: Optional[dict] = None) -> list[dict]:
    """The treaty's rules as they stand — see treaty_rules.merge."""
    st = st if st is not None else (_tr().load_structure(s, c.id, names=False) or {})
    su = setup_of(s, c)
    if not st:
        return []
    return trr.merge(su.rules if su is not None else None,
                     trr.derive(st, c.inception_dt, c.expiry_dt, c.commercial_terms))


def own_layout(su: Optional[CededSetup]) -> Optional[dict]:
    """The setup's own input file layout, when it reads from one."""
    lay = (su.input_layout if su is not None else None) or {}
    return lay if lay.get("source") == "own" else None


def _own_inputs(s, c: Contract, period: Optional[str] = None,
                *, up_to: bool = False) -> list[CededInput]:
    """The latest of the carrier's own files for each period — or only those
    for `period` (with up_to, every period of the treaty year up to it)."""
    freq = _frequency(setup_of(s, c))
    latest: dict[str, CededInput] = {}
    for ci in (s.query(CededInput).filter(CededInput.contract_id == c.id)
               .order_by(CededInput.version_no.asc(), CededInput.id.asc())):
        latest[ci.period] = ci
    out = []
    for p_, ci in latest.items():
        b = ce.bucket(p_, freq)
        if period is None or (b <= period if up_to else b == period):
            out.append(ci)
    return sorted(out, key=lambda x: x.period)


def setup_key(su: Optional[CededSetup]) -> str:
    """A fingerprint of everything in the setup that shapes a statement: its
    columns and their mapping, the input and output templates, the treaty's
    rules, the format, the programmes and the frequency."""
    if su is None:
        return "none"
    raw = json.dumps({"c": su.columns, "l": su.layout, "i": su.input_layout,
                      "r": su.rules, "f": su.file_format, "p": su.program_ids,
                      "q": su.frequency,
                      "b": hashlib.sha256(su.layout_blob or b"").hexdigest()[:12]},
                     sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def data_part(key: Optional[str]) -> str:
    """The input-data half of a statement's key — what 'the bordereaux
    changed' is judged on, whatever happened to the setup."""
    return (key or "").split("|")[0]


def statement_key(s, tid: int, c: Contract, period: str) -> str:
    """What a statement is built from: the input data, and the setup."""
    return f"{source_key_for(s, tid, c, period)}|{setup_key(setup_of(s, c))}"


def source_key_for(s, tid: int, c: Contract, period: str) -> str:
    """What the statement for `period` is read from, as one key — a change in
    it is what makes a refresh or a new version."""
    files = _own_inputs(s, c, period)
    if own_layout(setup_of(s, c)) or files:
        raw = ",".join(str(ci.id) for ci in files)
        return "own:" + hashlib.sha256(raw.encode()).hexdigest()[:20]
    return _source_key(_sources(s, tid, c, period))


def treaty_periods(s, tid: int, c: Contract) -> list[str]:
    """The reporting periods this treaty has inward bordereaux for, within its
    term, newest first."""
    out = set()
    freq = _frequency(setup_of(s, c))
    if own_layout(setup_of(s, c)):
        return sorted({ce.bucket(ci.period, freq) for ci in _own_inputs(s, c)},
                      reverse=True)
    for _o, v in _live_exports(s, tid, _protected_ids(s, c)):
        p = v.get("reporting_period")
        if p and ce.period_in_term(p, c.inception_dt, c.expiry_dt):
            out.add(ce.bucket(p, freq))
    # …and any period a file of the carrier's own was uploaded for.
    out |= {ce.bucket(ci.period, freq) for ci in _own_inputs(s, c)}
    return sorted(out, reverse=True)


def _sources(s, tid: int, c: Contract, period: str,
             *, up_to: bool = False) -> list[tuple[OutputExport, dict]]:
    """The live inward bordereaux for `period` — or, with up_to, for every
    period of the treaty year up to it (stop loss)."""
    out = []
    freq = _frequency(setup_of(s, c))
    for o, v in _live_exports(s, tid, _protected_ids(s, c)):
        p = v.get("reporting_period")
        if not p:
            continue
        b = ce.bucket(p, freq)
        if up_to:
            if b <= period and ce.period_in_term(p, c.inception_dt, c.expiry_dt):
                out.append((o, v))
        elif b == period:
            out.append((o, v))
    return out


def _rows_of(s, o: OutputExport, names: dict) -> tuple[list[dict], list[str]]:
    import storage
    tmpl = s.get(ExportTemplate, o.template_id) if o.template_id else None
    if tmpl is None:
        return [], ["no output template, so its columns cannot be read"]
    data = storage.resolve_bytes(o.blob_ref, o.blob)
    rows, notes = ce.read_export_rows(tmpl.structure or {}, data, o.filename or "")
    src = {"export_id": o.id, "programme": names.get(("p", o.program_id)),
           "broker": names.get(("b", o.broker_party_id))}
    for r in rows:
        r["_source"] = src
    return rows, notes


def _names(s, exports: list[OutputExport]) -> dict:
    progs = {p.id: p.name for p in s.query(Program).filter(
        Program.id.in_({o.program_id for o in exports if o.program_id} or {0}))}
    brokers = {p.id: p.legal_name for p in s.query(Party).filter(
        Party.id.in_({o.broker_party_id for o in exports if o.broker_party_id} or {0}))}
    return {**{("p", k): v for k, v in progs.items()},
            **{("b", k): v for k, v in brokers.items()}}


def _source_key(sources: list[tuple[OutputExport, dict]]) -> str:
    raw = ",".join(str(o.id) for o, _v in sorted(sources, key=lambda x: x[0].id))
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


# =============================================================================
#  Building a version
# =============================================================================

def decisions_for(s, c: Contract, period: str) -> dict[str, dict]:
    """What was decided on this treaty's exceptions for `period`, by key."""
    rows = (s.query(CededDecision, AppUser.full_name)
            .outerjoin(AppUser, AppUser.id == CededDecision.decided_by_user_id)
            .filter(CededDecision.contract_id == c.id, CededDecision.period == period)
            .all())
    return {d.key: {"kind": d.kind, "value": d.new_value, "reason": d.reason,
                    "by": who, "at": _iso(d.decided_at)} for d, who in rows}


def _checked(s, c: Contract, period: str, st: dict,
             rows: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
    """The treaty's rules run on every row, each exception marked with what
    was decided on it — and the rows the statement is made from: approved
    and fixed values written in, excluded rows out. The rows are checked
    again with those values (the broker's Fix & Validate): a value that still
    breaks its rule is open again."""
    rules = rules_for(s, c, st)
    decisions = decisions_for(s, c, period)
    checks = trr.check(rules, rows)
    out, fixes = trr.decide(checks, decisions)
    fixed = trr.apply_fixes(rows, fixes)
    if fixes:
        again = {(x["id"], it["row"]) for x in trr.check(rules, fixed)
                 for it in x.get("items") or []}
        refail = {(x["id"], it["row"]) for x in checks for it in x.get("items") or []
                  if it.get("status") in ("approved", "fixed")
                  and (x["id"], it["row"]) in again}
        if refail:
            trr.decide(checks, decisions, refail)
    notes = []
    if fixes:
        notes.append(f"{len(fixes)} row{'s' if len(fixes) != 1 else ''} corrected by "
                     f"your decisions on the treaty's rules.")
    if out:
        notes.append(f"{len(out)} row{'s' if len(out) != 1 else ''} left out of the "
                     f"statement by your decisions on the treaty's rules.")
    return checks, [r for i, r in enumerate(fixed) if i not in out], notes


def live_checks(s, c: Contract, cb: CededBordereau) -> tuple[list[dict], int]:
    """The statement's exceptions with what is decided on them NOW — and how
    many of those decisions are not in the statement yet (saved since it was
    prepared: Fix & Validate takes them in)."""
    import copy
    built = cb.checks or []
    checks = copy.deepcopy(built)
    decisions = decisions_for(s, c, cb.period)
    # A value checked again and still failing stays open while it is unchanged.
    refail = {(x["id"], it["row"]) for x in built for it in x.get("items") or []
              if it.get("still_fails")
              and (decisions.get(it.get("key")) or {}).get("value") == it.get("new_value")}
    trr.decide(checks, decisions, refail)
    was = {it.get("key"): (it.get("status"), it.get("new_value"))
           for x in built for it in x.get("items") or []}
    unapplied = sum(1 for x in checks for it in x.get("items") or []
                    if (it.get("status"), it.get("new_value")) != was.get(it.get("key")))
    return checks, unapplied


def gather(s, c: Contract, period: str, st: dict) -> dict:
    """Read the inward bordereaux for `period` and cede them under `st`.
    Writes nothing — build() saves it, the setup preview just shows it."""
    tr = _tr()
    tid = c.tenant_id
    own = own_layout(setup_of(s, c))
    # A file uploaded for this period stands in for the brokers' bordereaux.
    if own or _own_inputs(s, c, period):
        return _gather_own(s, c, period, st, own)
    sources = _sources(s, tid, c, period)
    ytd = _sources(s, tid, c, period, up_to=True) \
        if st["treaty_type"] == "stop_loss" else None
    all_exports = [o for o, _v in (ytd or sources)]
    names = _names(s, all_exports)
    rows: list[dict] = []
    notes: list[str] = []
    src_meta = []
    for o, v in sources:
        r, n = _rows_of(s, o, names)
        rows.extend(r)
        cov = ce.coverage_of(r)
        src_meta.append({"export_id": o.id, "program_id": o.program_id,
                         "programme": names.get(("p", o.program_id)),
                         "broker": names.get(("b", o.broker_party_id)),
                         "filename": o.filename, "period": v.get("reporting_period"),
                         "version_no": v.get("version_no"), "rows": len(r),
                         "has_premium": cov["premium"],
                         "has_sum_insured": cov["sum_insured"],
                         "has_claims": cov["claims"], "notes": n})
        for msg in n:
            notes.append(f"{names.get(('p', o.program_id)) or 'A programme'}: {msg}")
    ytd_rows = None
    if ytd is not None:
        ytd_rows = []
        for o, _v in ytd:
            ytd_rows.extend(_rows_of(s, o, names)[0])

    checks, rows, left_out = _checked(s, c, period, st, rows)
    notes.extend(left_out)
    result = ce.compute(st, rows, ytd_rows=ytd_rows)
    # A line keeps only the inward fields the setup's layout passes through.
    su = setup_of(s, c)
    keep = cl.needed_fields(su.columns if su is not None else None)
    cl.prune(result["lines"], keep, cl.needed_cols(su.columns if su is not None else None))
    cl.prune(result["skipped"], set())
    notes.extend(result["notes"])
    have = {m["program_id"] for m in src_meta}
    feeds = set(_protected_ids(s, c))
    missing = [p for p in tr.programmes(s, c)
               if p["id"] in feeds and p["id"] not in have]
    return {"sources": sources, "result": result, "notes": notes,
            "src_meta": src_meta, "missing": missing, "checks": checks}


def _own_rows(s, ci: CededInput, layout: dict) -> list[dict]:
    try:
        rows = cl.read_input(layout, ci.blob or b"", ci.filename or "")
    except Exception as e:  # noqa: BLE001
        log.warning("own input %s unreadable: %s", ci.id, e)
        return []
    for r in rows:
        r["_source"] = {"export_id": None, "programme": None, "broker": None}
    return rows


def _gather_own(s, c: Contract, period: str, st: dict, layout: Optional[dict]) -> dict:
    """gather(), for a setup that reads the carrier's own file."""
    files = _own_inputs(s, c, period)
    rows: list[dict] = []
    src_meta = []
    for ci in files:
        r = _own_rows(s, ci, ci.layout or layout or {})
        rows.extend(r)
        cov = ce.coverage_of(r)
        src_meta.append({"export_id": None, "input_id": ci.id, "program_id": None,
                         "programme": "Your file", "broker": None,
                         "filename": ci.filename, "period": ci.period,
                         "version_no": ci.version_no, "rows": len(r),
                         "has_premium": cov["premium"],
                         "has_sum_insured": cov["sum_insured"],
                         "has_claims": cov["claims"], "notes": []})
    ytd_rows = None
    if st["treaty_type"] == "stop_loss":
        ytd_rows = []
        for ci in _own_inputs(s, c, period, up_to=True):
            ytd_rows.extend(_own_rows(s, ci, ci.layout or layout or {}))
    checks, rows, left_out = _checked(s, c, period, st, rows)
    result = ce.compute(st, rows, ytd_rows=ytd_rows)
    su = setup_of(s, c)
    cl.prune(result["lines"], cl.needed_fields(su.columns if su is not None else None),
             cl.needed_cols(su.columns if su is not None else None))
    cl.prune(result["skipped"], set())
    notes = left_out + list(result["notes"])
    if not files:
        notes.insert(0, f"No file of yours has been uploaded for {period} yet.")
    return {"sources": [], "result": result, "notes": notes,
            "src_meta": src_meta, "missing": [], "checks": checks}


def build(s, c: Contract, period: str, user_id: Optional[int], *,
          auto: bool = False) -> CededBordereau:
    """Prepare (or refresh) the statement for one treaty and one period.

    The latest version is REFRESHED while it is a draft. Once sent, a change
    in the inward bordereaux makes the NEXT version, carrying what was sent
    before so each reinsurer sees the difference. Unchanged inputs change
    nothing."""
    tr = _tr()
    st = tr.load_structure(s, c.id, names=False)
    if not st:
        raise HTTPException(409, "this treaty has no structure yet, so nothing "
                                 "can be ceded under it")
    if active_setup(s, c) is None:
        raise HTTPException(409, "set up this treaty's reinsurance bordereau "
                                 "first — which programmes feed it, how often, "
                                 "its layout and who receives it")
    tid = c.tenant_id
    key = statement_key(s, tid, c, period)
    latest = (s.query(CededBordereau)
              .filter(CededBordereau.contract_id == c.id,
                      CededBordereau.period == period)
              .order_by(CededBordereau.version_no.desc()).first())
    if latest and latest.status != "superseded":
        # A draft follows its setup: same data AND same setup, or it is
        # rebuilt. A sent statement is only re-issued for new data — a setup
        # changed afterwards applies from the next statement.
        same = (latest.source_key == key if latest.status in OPEN
                else data_part(latest.source_key) == data_part(key))
        if same and (auto or latest.status not in OPEN):
            return latest

    g = gather(s, c, period, st)
    result, notes, src_meta, missing = (g["result"], g["notes"],
                                        g["src_meta"], g["missing"])
    currency = (st.get("terms") or {}).get("currency")

    refresh = latest is not None and latest.status in OPEN
    if refresh:
        cb = latest
    else:
        # The next version points back at the one that was SENT, so each
        # reinsurer's new statement can show the difference from it.
        cb = CededBordereau(
            tenant_id=tid, contract_id=c.id, period=period,
            version_no=(latest.version_no + 1) if latest else 1,
            previous_id=(latest.id if latest and latest.status != "superseded"
                         else None))
        s.add(cb)
    cb.status = "draft"
    cb.currency = currency
    cb.totals = result["totals"]
    cb.notes = notes
    cb.skipped = result["skipped"]
    cb.lines = result["lines"]
    cb.sources = src_meta
    cb.missing = missing
    cb.checks = g.get("checks") or []
    cb.source_key = key
    cb.auto_prepared = auto
    cb.built_at = _now()
    cb.built_by_user_id = user_id
    s.flush()
    _write_statements(s, c, cb, st)
    return cb


def _write_statements(s, c: Contract, cb: CededBordereau, st: dict) -> None:
    """Each reinsurer's share, totals and workbook — rewritten whole."""
    s.query(CededStatement).filter(CededStatement.cb_id == cb.id).delete()
    split = ce.by_reinsurer(st, cb.lines or [])
    prev_totals: dict[int, dict] = {}
    if cb.previous_id:
        prev_totals = {x.reinsurer_party_id: x.totals for x in s.query(CededStatement)
                       .filter(CededStatement.cb_id == cb.previous_id)}
    parties = {p.id: p.legal_name for p in s.query(Party).filter(
        Party.id.in_(set(split) or {0}))}
    carrier = _tr()._carrier_name(s, c)
    summ = tt.summarise(st) or {}
    su = setup_of(s, c)
    columns = (su.columns if su is not None and su.columns
               else ce.default_columns(st.get("treaty_type")))
    fmt = (su.file_format if su is not None else None) or "xlsx"
    layout = (su.layout if su is not None else None) or {}
    own = layout.get("source") in ("uploaded", "standard")
    for pid, part in split.items():
        name = parties.get(pid) or f"Reinsurer {pid}"
        ctx = cl.context(period=cb.period, treaty=c.name or f"Treaty {c.id}",
                         reinsured=carrier, reinsurer=name, currency=cb.currency)
        if own and fmt == "csv":
            data = cl.fill_csv(columns, part["lines"], ctx)
        elif own:
            data = cl.fill_xlsx(
                blob=su.layout_blob, layout=layout, columns=columns, ctx=ctx,
                lines=part["lines"], summary={"rows": _summary_rows(
                    carrier, name, c, summ, cb, part, prev_totals.get(pid))})
        elif fmt == "csv":
            data = ce.statement_csv(part["lines"], columns)
        else:
            data = ce.statement_xlsx(
                carrier=carrier, reinsurer=name, treaty=c.name or f"Treaty {c.id}",
                type_label=summ.get("type_label") or "", period=cb.period,
                version=cb.version_no, currency=cb.currency, shares=part["shares"],
                totals=part["totals"], lines=part["lines"], notes=cb.notes or [],
                previous=prev_totals.get(pid), columns=columns)
        safe = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in name)
        s.add(CededStatement(
            cb_id=cb.id, contract_id=c.id, tenant_id=c.tenant_id,
            reinsurer_party_id=pid, shares=part["shares"], totals=part["totals"],
            previous_totals=prev_totals.get(pid),
            filename=f"{c.name or 'Treaty'} - {cb.period} - {safe} - v{cb.version_no}.{fmt}",
            blob=data, status="pending"))
    s.flush()


def _summary_rows(carrier, reinsurer, c, summ, cb, part, previous) -> list:
    """The sheet added after a carrier's own layout: who, what, and the totals
    the lines add up to."""
    heads = {k: h for k, h, _kind in ce.LINE_COLUMNS}
    rows = [("Reinsured", carrier), ("Reinsurer", reinsurer),
            ("Treaty", c.name or f"Treaty {c.id}"), ("Type", summ.get("type_label")),
            ("Period", cb.period), ("Version", cb.version_no),
            ("Currency", cb.currency or "as written"),
            ("Your share", ", ".join(f"{k}: {v:g}%" for k, v in part["shares"].items())),
            (None, None)]
    for k in ce.MONEY_KEYS:
        if part["totals"].get(k) is not None:
            rows.append((heads[k], part["totals"][k]))
            if previous and previous.get(k) is not None:
                rows.append((f"{heads[k]}, previously sent", previous[k]))
    rows.append(("Lines", part["totals"].get("lines")))
    for n in cb.notes or []:
        rows.append((None, n))
    return rows


def prepare_due(s, tid: int, auto_send: Optional[list] = None) -> int:
    """Prepare every statement that is ready, for every treaty with an ACTIVE
    setup: a period with inward bordereaux in and no statement yet, or a sent
    one whose inward files changed since. Returns how many were prepared.

    A setup on automatic sending has its new drafts appended to `auto_send`
    when every programme feeding it has its bordereau in — the caller sends
    them after committing."""
    tr = _tr()
    made = 0
    for c in s.query(Contract).filter(Contract.tenant_id == tid,
                                      Contract.contract_type == tr.REINSURER):
        if tr._state(c) in tr.CLOSED_STATES or not tr.load_structure(s, c.id, names=False):
            continue
        su = active_setup(s, c)
        if su is None:
            continue
        for period in treaty_periods(s, tid, c):
            latest = (s.query(CededBordereau)
                      .filter(CededBordereau.contract_id == c.id,
                              CededBordereau.period == period)
                      .order_by(CededBordereau.version_no.desc()).first())
            key = statement_key(s, tid, c, period)
            if latest and latest.status in OPEN and latest.source_key == key:
                continue                  # a draft waits for the carrier
            if latest and latest.status not in OPEN \
                    and data_part(latest.source_key) == data_part(key):
                continue                  # sent, and its data has not changed
            try:
                with s.begin_nested():
                    cb = build(s, c, period, None, auto=True)
                made += 1
                if (auto_send is not None and su.send_mode == "auto"
                        and cb.status == "draft" and not cb.missing):
                    auto_send.append(cb.id)
            except Exception as e:  # noqa: BLE001 — one treaty never blocks the list
                log.warning("auto-prepare %s %s failed: %s", c.id, period, e)
    return made


def _rollup(cb: CededBordereau, statements: list[CededStatement]) -> str:
    if cb.status in ("draft", "superseded") or not statements:
        return cb.status
    st = {x.status for x in statements}
    if "queried" in st:
        return "queried"
    if st == {"agreed"}:
        return "agreed"
    return "sent"


# =============================================================================
#  JSON shapes
# =============================================================================

def _summary(s, cb: CededBordereau, c: Contract, statements=None) -> dict:
    statements = statements if statements is not None else s.query(
        CededStatement).filter(CededStatement.cb_id == cb.id).all()
    su = setup_of(s, c)
    due = due_date(cb.period, su.due_days if su is not None else None)
    sent = cb.status not in ("draft",)
    return {
        "id": cb.id, "contract_id": c.id, "treaty": c.name or f"Treaty {c.id}",
        "type_label": tt.label((_tr().load_structure(s, c.id, names=False) or {})
                               .get("treaty_type")),
        "period": cb.period, "version_no": cb.version_no, "status": cb.status,
        "currency": cb.currency, "totals": cb.totals or {},
        "reinsurers": len(statements),
        "agreed": sum(1 for x in statements if x.status == "agreed"),
        "queried": sum(1 for x in statements if x.status == "queried"),
        "auto_prepared": bool(cb.auto_prepared),
        "is_correction": bool(cb.previous_id),
        "missing_programmes": len(cb.missing or []),
        "skipped": len(cb.skipped or []) if cb.skipped is not None else 0,
        "due_on": due.isoformat() if due else None,
        "overdue": bool(due and not sent and dt.date.today() > due),
        "built_at": _iso(cb.built_at), "sent_at": _iso(cb.sent_at),
    }


def _statement_json(x: CededStatement, names: dict) -> dict:
    return {"id": x.id, "reinsurer_party_id": x.reinsurer_party_id,
            "reinsurer": names.get(x.reinsurer_party_id), "shares": x.shares or {},
            "totals": x.totals or {}, "previous_totals": x.previous_totals,
            "filename": x.filename, "status": x.status,
            "sent_at": _iso(x.sent_at), "sent_to": x.sent_to or [],
            "answered_at": _iso(x.answered_at), "answered_by": x.answered_by,
            "answer_note": x.answer_note}


def _carrier_cb(s, p: Principal, cb_id: int) -> tuple[CededBordereau, Contract]:
    cr = _cr()
    cr._require_carrier(p)
    cb = s.get(CededBordereau, cb_id)
    if cb is None:
        raise HTTPException(404, "not found")
    c = cr._contract_access(s, p, cb.contract_id)
    return cb, c


# =============================================================================
#  Carrier routes
# =============================================================================

@router.get("/reinsurance/bordereaux")
def list_ceded(contract_id: Optional[int] = None, status: Optional[str] = None,
               p: Principal = Depends(current_principal)):
    """Every statement, newest first, after preparing whatever is due."""
    _cr()._require_carrier(p)
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, p)
        auto: list[int] = []
        prepared = prepare_due(s, tid, auto)
        s.commit()
        q = (s.query(CededBordereau, Contract)
             .join(Contract, Contract.id == CededBordereau.contract_id)
             .filter(CededBordereau.tenant_id == tid))
        if contract_id:
            q = q.filter(CededBordereau.contract_id == contract_id)
        rows = q.order_by(CededBordereau.period.desc(), Contract.name,
                          CededBordereau.version_no.desc()).all()
        stmts: dict[int, list] = {}
        for x in s.query(CededStatement).filter(
                CededStatement.cb_id.in_([cb.id for cb, _c in rows] or [0])):
            stmts.setdefault(x.cb_id, []).append(x)
        items = [_summary(s, cb, c, stmts.get(cb.id, [])) for cb, c in rows]
        if status:
            items = [i for i in items if i["status"] == status
                     or (status == "overdue" and i["overdue"])]
        counts = {k: sum(1 for i in items if i["status"] == k)
                  for k in ("draft", "sent", "queried", "agreed", "superseded")}
        counts["overdue"] = sum(1 for i in items if i["overdue"])
        treaties, needs_setup = [], []
        for c in (s.query(Contract).filter(Contract.tenant_id == tid,
                                           Contract.contract_type == _tr().REINSURER)
                  .order_by(Contract.name)):
            if _tr()._state(c) in _tr().CLOSED_STATES:
                continue
            su = setup_of(s, c)
            row = {"contract_id": c.id, "name": c.name or f"Treaty {c.id}",
                   "setup_status": su.status if su is not None else None,
                   # "own": its statements are read from a file the carrier
                   # uploads each period, so preparing one asks for it.
                   "input_source": "own" if own_layout(su) else "brokers",
                   "input_file": (own_layout(su) or {}).get("filename"),
                   "output": ((su.layout or {}).get("source") if su is not None else None)
                   or "kavachio",
                   "output_file": ((su.layout or {}).get("filename")
                                   if su is not None else None),
                   "frequency": _frequency(su)}
            (treaties if su is not None and su.status == "active"
             else needs_setup).append(row)
    sent_auto = 0
    for cb_id in auto:
        try:
            deliver(cb_id, None, None, None)
            sent_auto += 1
        except Exception as e:  # noqa: BLE001
            log.warning("automatic send of ceded bordereau %s failed: %s", cb_id, e)
    if sent_auto:
        return list_ceded(contract_id, status, p)
    return {"items": items, "counts": counts, "prepared_now": prepared,
            "treaties": treaties, "needs_setup": needs_setup}


@router.get("/reinsurance/bordereaux/periods/{contract_id}")
def periods_for(contract_id: int, p: Principal = Depends(current_principal)):
    cr = _cr()
    cr._require_carrier(p)
    with SessionLocal() as s:
        c = cr._contract_access(s, p, contract_id)
        return {"periods": treaty_periods(s, c.tenant_id, c)}


class PrepareIn(BaseModel):
    contract_id: int
    period: str


@router.post("/reinsurance/bordereaux")
def prepare(body: PrepareIn, p: Principal = Depends(current_principal)):
    """Prepare (or refresh) one treaty's statement for one period."""
    cr = _cr()
    cr._require_carrier(p)
    period = (body.period or "").strip()
    if not period:
        raise HTTPException(400, "choose a period")
    with SessionLocal() as s:
        c = cr._contract_access(s, p, body.contract_id)
        if (c.contract_type or "") != _tr().REINSURER:
            raise HTTPException(400, "that is not a reinsurance treaty")
        cb = build(s, c, period, p.user_id)
        s.commit()
        return detail(cb.id, p)


@router.get("/reinsurance/bordereaux/{cb_id}")
def detail(cb_id: int, p: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        cb, c = _carrier_cb(s, p, cb_id)
        stmts = (s.query(CededStatement).filter(CededStatement.cb_id == cb.id)
                 .order_by(CededStatement.id).all())
        names = {x.id: x.legal_name for x in s.query(Party).filter(
            Party.id.in_({y.reinsurer_party_id for y in stmts} or {0}))}
        current_key = statement_key(s, c.tenant_id, c, cb.period)
        versions = [{"id": v.id, "version_no": v.version_no, "status": v.status,
                     "sent_at": _iso(v.sent_at)}
                    for v in s.query(CededBordereau)
                    .filter(CededBordereau.contract_id == c.id,
                            CededBordereau.period == cb.period)
                    .order_by(CededBordereau.version_no.desc())]
        st = _tr().load_structure(s, c.id, names=False) or {}
        su = setup_of(s, c)
        checks, unapplied = live_checks(s, c, cb)
        stop = [x["label"] for x in trr.critical(cb.checks)]
        # What Send would do, said before it is pressed: each reinsurer, who
        # it goes to, and the BDX file attached to the email.
        send_plan = [{"statement_id": x.id, "reinsurer": names.get(x.reinsurer_party_id),
                      "reinsurer_party_id": x.reinsurer_party_id,
                      "to": [{"name": ct.full_name, "email": ct.email}
                             for ct in recipients_for(s, su, x.reinsurer_party_id)],
                      "attachment": x.filename,
                      "attachment_bytes": len(x.blob or b"")} for x in stmts]
        return {
            **_summary(s, cb, c, stmts),
            "send_plan": send_plan,
            "cc": [e for e in ((su.cc or []) if su else []) if (e or "").strip()],
            "summary_headline": (tt.summarise(st) or {}).get("headline"),
            "notes": cb.notes or [],
            "lines": (cb.lines or [])[:2000],
            "lines_total": len(cb.lines or []),
            "skipped": (cb.skipped or [])[:500],
            "sources": cb.sources or [],
            "missing": cb.missing or [],
            "statements": [_statement_json(x, names) for x in stmts],
            "versions": versions,
            # Inward files changed since this was built.
            "stale": cb.status != "superseded"
                     and data_part(current_key) != data_part(cb.source_key),
            # A draft built before its setup last changed — rebuilt the next
            # time the statements list is opened, or by Prepare again.
            "setup_changed": cb.status in OPEN and current_key != cb.source_key,
            # The treaty's rules the rows broke, with what is decided on each
            # row now; a Critical one stops Send while a row of it is open.
            "checks": checks,
            # Decisions saved since it was prepared — Fix & Validate takes them in.
            "unapplied": unapplied,
            # Every rule the rows were checked against — broken or not — for
            # the exceptions' Check Summary.
            "rules": [{k: r.get(k) for k in ("id", "label", "kind", "subject",
                                             "severity", "source", "clause")}
                      for r in (rules_for(s, c, st) if st else []) if r.get("enabled", True)],
            "send_blocked_by": stop,
            "can_send": cb.status == "draft" and bool(stmts) and not stop
                        and not unapplied and _cr()._speaks_for_carrier(p),
            "columns": [{"key": k, "label": h, "kind": kind}
                        for k, h, kind in ce.LINE_COLUMNS],
        }


@router.get("/reinsurance/bordereaux/{cb_id}/statements/{sid}/file")
def statement_file(cb_id: int, sid: int, p: Principal = Depends(current_principal)):
    cr = _cr()
    with SessionLocal() as s:
        cb, _c = _carrier_cb(s, p, cb_id)
        x = s.get(CededStatement, sid)
        if x is None or x.cb_id != cb.id:
            raise HTTPException(404, "not found")
        data, name = x.blob, x.filename or "statement.xlsx"
    return Response(content=data or b"", media_type=cr._media_type(name),
                    headers={"Content-Disposition": cr._content_disposition(name)})


class SendIn(BaseModel):
    note: Optional[str] = None


def recipients_for(s, su: Optional[CededSetup], party_id: int) -> list[PartyContact]:
    """Who a reinsurer's statement is emailed to: the contacts the setup
    names for it, else every contact of theirs with an email."""
    contacts = [ct for ct in s.query(PartyContact).filter(
        PartyContact.party_id == party_id) if (ct.email or "").strip()]
    want = {str(k): v for k, v in ((su.recipients or {}) if su else {}).items()} \
        .get(str(party_id))
    if want:
        contacts = [ct for ct in contacts if ct.id in {int(i) for i in want}]
    return contacts


def deliver(cb_id: int, user_id: Optional[int], note: Optional[str],
            actor: Optional[str]) -> dict:
    """Send every reinsurer its statement: emailed to the contacts the setup
    names (every contact with an email, when it names none), copied to the
    setup's cc, and shown on the reinsurer's private link. The version it
    replaces is superseded. Used by the Send button and by automatic sending.
    """
    import treaty_email
    results = []
    with SessionLocal() as s:
        cb = s.get(CededBordereau, cb_id)
        c = s.get(Contract, cb.contract_id)
        if cb.status != "draft":
            raise HTTPException(409, f"this statement is {cb.status}; prepare it "
                                     f"again to send a new version")
        stmts = s.query(CededStatement).filter(CededStatement.cb_id == cb.id).all()
        if cb.status in OPEN and live_checks(s, c, cb)[1]:
            raise HTTPException(409, "decisions on the treaty's exceptions are not in "
                                     "this statement yet — run Fix & Validate first")
        stop = trr.critical(cb.checks)
        if stop:
            raise HTTPException(409, "the treaty's rules stop this being sent: "
                                     + "; ".join(x["label"] for x in stop)
                                     + ". Decide each row on its Exceptions — "
                                       "Approve, Fix or Dismiss it — then Fix & "
                                       "Validate.")
        if not stmts:
            raise HTTPException(409, "there is nothing to send — no reinsurer "
                                     "holds a share of this treaty")
        su = setup_of(s, c)
        cc = [e for e in ((su.cc or []) if su else []) if (e or "").strip()]
        carrier = _tr()._carrier_name(s, c)
        names = {x.id: x.legal_name for x in s.query(Party).filter(
            Party.id.in_({y.reinsurer_party_id for y in stmts}))}
        now = _now()
        outbox = []
        for x in stmts:
            contacts = recipients_for(s, su, x.reinsurer_party_id)
            x.status = "sent"
            x.sent_at = now
            x.sent_to = [ct.email for ct in contacts]
            for ct in contacts:
                outbox.append((ct.email, ct.full_name, names.get(x.reinsurer_party_id),
                               x.filename, x.blob, x.totals, x.previous_totals))
            results.append({"reinsurer": names.get(x.reinsurer_party_id),
                            "to": [ct.email for ct in contacts]})
        cb.status = "sent"
        cb.sent_at = now
        cb.sent_by_user_id = user_id
        if cb.previous_id:
            prev = s.get(CededBordereau, cb.previous_id)
            if prev is not None:
                prev.status = "superseded"
        s.commit()
        treaty, period, version, cur = c.name or f"Treaty {c.id}", cb.period, \
            cb.version_no, cb.currency
        tid, cid = c.tenant_id, c.id
    emailed = 0
    for to, who, org, fname, data, totals, prev in outbox:
        html = treaty_email.statement_email(
            name=who or to, carrier=carrier, reinsurer=org or "", treaty=treaty,
            period=period, version=version, currency=cur, totals=totals or {},
            previous=prev, note=note, filename=fname)
        mime = ("text/csv" if (fname or "").endswith(".csv") else
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        ok = treaty_email.safe_send(
            to, f"{carrier} — reinsurance bordereau {period} — {treaty}"
                + (f" (version {version})" if version > 1 else ""),
            html, text=f"Your reinsurance bordereau for {treaty}, {period}, is "
                       f"attached: {fname}.",
            attachments=[(fname, data or b"", mime)], cc=cc)
        emailed += 1 if ok else 0
    try:
        from audit import log_activity
        log_activity(tid, actor or "Kavachio (automatic)",
                     "reinsurance_bordereau_sent", target=f"contract:{cid}",
                     details={"ceded_bordereau_id": cb_id, "period": period,
                              "version": version, "reinsurers": len(results),
                              "automatic": user_id is None},
                     actor_user_id=user_id)
    except Exception:  # noqa: BLE001
        pass
    return {"emails": emailed, "of": len(outbox),
            "no_contact": [r["reinsurer"] for r in results if not r["to"]]}


@router.post("/reinsurance/bordereaux/{cb_id}/send")
def send(cb_id: int, body: SendIn = SendIn(),
         p: Principal = Depends(current_principal)):
    """Send this statement to every reinsurer on the treaty — see deliver."""
    cr = _cr()
    cr._assert_speaks_for_carrier(p, "send a reinsurance bordereau")
    with SessionLocal() as s:
        _carrier_cb(s, p, cb_id)
        actor = cr._principal_email_for(s, p)
    sent = deliver(cb_id, p.user_id, body.note, actor)
    out = detail(cb_id, p)
    out["sent"] = sent
    return out


class DecisionIn(BaseModel):
    key: str
    kind: str                       # approve | fix | dismiss | reopen (exclude kept)
    value: Optional[str] = None     # approve: one of the choices; fix: the value
    reason: Optional[str] = None


class DecideIn(BaseModel):
    decisions: list[DecisionIn]


@router.post("/reinsurance/bordereaux/{cb_id}/decide")
def decide(cb_id: int, body: DecideIn, p: Principal = Depends(current_principal)):
    """Decide the rows a treaty rule caught — the broker's exception
    resolution, for a reinsurance bordereau: `approve` (the value the rule
    recommends), `fix` (a value entered), `dismiss` (kept as it is), `reopen`
    (undecided again). Recorded only: Fix & Validate prepares the statement
    again with them, as the broker's does."""
    cr = _cr()
    cr._assert_speaks_for_carrier(p, "decide a reinsurance bordereau's exceptions")
    done, skipped = 0, []
    with SessionLocal() as s:
        cb, c = _carrier_cb(s, p, cb_id)
        if cb.status not in OPEN:
            raise HTTPException(409, f"this statement is {cb.status} — what was "
                                     f"sent stays as it was. Decide on the draft "
                                     f"that replaces it.")
        items = {it["key"]: (chk, it) for chk in cb.checks or []
                 for it in chk.get("items") or [] if it.get("key")}
        for d in body.decisions:
            if d.kind not in (*trr.DECISIONS, "reopen"):
                raise HTTPException(400, f"unknown decision {d.kind!r}")
            hit = items.get(d.key)
            if hit is None:
                skipped.append(d.key)
                continue
            chk, it = hit
            who = it.get("policy") or "this row"
            value = (d.value or "").strip() or None
            if d.kind == "approve":
                value = value if value in (chk.get("options") or []) else chk.get("recommendation")
                if value is None:
                    raise HTTPException(400, f"“{chk.get('label')}” recommends no value "
                                             f"for {who} — use Fix to enter one")
            if d.kind == "fix":
                if value is None:
                    raise HTTPException(400, f"enter the corrected value for {who}")
                if chk.get("subject") in trr.NUMERIC and ce.num(value) is None:
                    raise HTTPException(400, f"{value!r} is not a number — the "
                                             f"{chk.get('field') or 'value'} of {who}")
            row = (s.query(CededDecision)
                   .filter(CededDecision.contract_id == c.id,
                           CededDecision.period == cb.period,
                           CededDecision.key == d.key).first())
            if d.kind == "reopen":
                if row is not None:
                    s.delete(row)
            else:
                if row is None:
                    row = CededDecision(tenant_id=c.tenant_id, contract_id=c.id,
                                        period=cb.period, key=d.key)
                    s.add(row)
                row.rule_id = chk["id"]
                row.policy, row.value = it.get("policy"), it.get("value")
                row.kind = d.kind
                row.new_value = value if d.kind in ("approve", "fix") else None
                row.reason = (d.reason or "").strip() or None
                row.decided_by_user_id, row.decided_at = p.user_id, _now()
            done += 1
        s.commit()
    out = detail(cb_id, p)
    out.update({"updated": done, "skipped": skipped})
    return out


@router.post("/reinsurance/bordereaux/{cb_id}/validate")
def fix_and_validate(cb_id: int, p: Principal = Depends(current_principal)):
    """Fix & Validate: prepare the draft again with the decisions taken in —
    approved and fixed values written into the rows, excluded rows out — and
    check every row again."""
    cr = _cr()
    cr._assert_speaks_for_carrier(p, "validate a reinsurance bordereau")
    with SessionLocal() as s:
        cb, c = _carrier_cb(s, p, cb_id)
        if cb.status not in OPEN:
            raise HTTPException(409, f"this statement is {cb.status} — prepare the "
                                     f"next version to change it")
        build(s, c, cb.period, p.user_id)
        s.commit()
    return detail(cb_id, p)


# =============================================================================
#  The reinsurer's side, on its private link
# =============================================================================

def statements_for_link(s, contract_id: int, party_id: int) -> list[dict]:
    """The statements this reinsurer has been sent for this treaty."""
    rows = (s.query(CededStatement, CededBordereau)
            .join(CededBordereau, CededBordereau.id == CededStatement.cb_id)
            .filter(CededStatement.contract_id == contract_id,
                    CededStatement.reinsurer_party_id == party_id,
                    CededStatement.status != "pending")
            .order_by(CededBordereau.period.desc(), CededBordereau.version_no.desc())
            .all())
    return [{"id": x.id, "period": cb.period, "version_no": cb.version_no,
             "superseded": cb.status == "superseded", "currency": cb.currency,
             "shares": x.shares or {}, "totals": x.totals or {},
             "previous_totals": x.previous_totals, "status": x.status,
             "sent_at": _iso(x.sent_at), "answered_by": x.answered_by,
             "answered_at": _iso(x.answered_at), "answer_note": x.answer_note,
             "filename": x.filename} for x, cb in rows]


def _link_statement(s, token: str, session: str, sid: int):
    tr = _tr()
    link, c = tr._require_open(s, token, session)
    x = s.get(CededStatement, sid)
    if (x is None or x.contract_id != c.id or x.reinsurer_party_id != link.party_id
            or x.status == "pending"):
        raise HTTPException(404, "statement not found")
    return link, c, x


@public_router.get("/{token}/statements/{sid}/file")
def link_statement_file(token: str, sid: int, request: Request,
                        session: str = Query("", alias="session"),
                        x_treaty_session: str = Header(default="")):
    tr = _tr()
    cr = _cr()
    with SessionLocal() as s:
        link, _c, x = _link_statement(s, token, tr._session_value(x_treaty_session,
                                                                  session), sid)
        tr._event(s, link, "downloaded", actor=link.email, request=request,
                  detail={"statement_id": x.id, "filename": x.filename})
        s.commit()
        data, name = x.blob, x.filename or "statement.xlsx"
    return Response(content=data or b"", media_type=cr._media_type(name),
                    headers={"Content-Disposition": cr._content_disposition(name)})


class AnswerIn(BaseModel):
    name: str
    note: Optional[str] = None


def _answer(token: str, sid: int, body: AnswerIn, request: Request,
            session: str, status: str) -> dict:
    tr = _tr()
    name = (body.name or "").strip()
    note = (body.note or "").strip() or None
    if not name:
        raise HTTPException(400, "Type your name.")
    if status == "queried" and not note:
        raise HTTPException(400, "Say what the query is.")
    with SessionLocal() as s:
        link, c, x = _link_statement(s, token, session, sid)
        cb = s.get(CededBordereau, x.cb_id)
        if cb.status == "superseded":
            raise HTTPException(409, "A newer version of this statement has been "
                                     "sent — answer that one.")
        x.status = status
        x.answered_at = _now()
        x.answered_by = name
        x.answer_note = note
        fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        x.answer_ip = fwd or (request.client.host if request.client else None)
        tr._event(s, link, "statement_" + status, actor=link.email, request=request,
                  detail={"statement_id": x.id, "period": cb.period, "note": note})
        s.flush()
        cb.status = _rollup(cb, s.query(CededStatement)
                            .filter(CededStatement.cb_id == cb.id).all())
        s.commit()
        try:
            from audit import log_activity
            party = s.get(Party, link.party_id)
            log_activity(c.tenant_id, link.email,
                         "reinsurance_bordereau_" + status,
                         target=f"contract:{c.id}",
                         details={"period": cb.period, "reinsurer":
                                  getattr(party, "legal_name", None),
                                  "by": name, "note": note})
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "statements": statements_for_link(s, c.id, link.party_id)}


@public_router.post("/{token}/statements/{sid}/agree")
def link_statement_agree(token: str, sid: int, body: AnswerIn, request: Request,
                         session: str = Query("", alias="session"),
                         x_treaty_session: str = Header(default="")):
    return _answer(token, sid, body, request,
                   _tr()._session_value(x_treaty_session, session), "agreed")


@public_router.post("/{token}/statements/{sid}/query")
def link_statement_query(token: str, sid: int, body: AnswerIn, request: Request,
                         session: str = Query("", alias="session"),
                         x_treaty_session: str = Header(default="")):
    return _answer(token, sid, body, request,
                   _tr()._session_value(x_treaty_session, session), "queried")


# =============================================================================
#  The dashboard
# =============================================================================

def dashboard_counts(s, tid: int, period: Optional[str] = None) -> dict:
    """Ceded statements by state, for the Home dashboard. Superseded versions
    are history and are not counted.

    `period` is the dashboard's Monthly Activity pick — '2027-03', '2027', or
    None for every period. A statement counts when its own period (a month,
    quarter or year) overlaps it."""
    rows = (s.query(CededBordereau)
            .filter(CededBordereau.tenant_id == tid,
                    CededBordereau.status != "superseded").all())
    pick = ce.period_bounds(period) if period else None
    if pick:
        def overlaps(p_: str) -> bool:
            b = ce.period_bounds(p_)
            return bool(b) and b[0] <= pick[1] and b[1] >= pick[0]
        rows = [r for r in rows if overlaps(r.period)]
    month = dt.date.today().replace(day=1)
    sent_month = sum(1 for r in rows if r.sent_at
                     and (r.sent_at.date() if hasattr(r.sent_at, "date") else r.sent_at)
                     >= month)
    today = dt.date.today()
    tr = _tr()
    not_set_up = sum(
        1 for c in s.query(Contract).filter(Contract.tenant_id == tid,
                                            Contract.contract_type == tr.REINSURER)
        if tr._state(c) not in tr.CLOSED_STATES and active_setup(s, c) is None)
    return {
        "period": period,
        "total": len(rows),
        "not_set_up": not_set_up,
        "ready_to_send": sum(1 for r in rows if r.status == "draft"),
        "sent": sum(1 for r in rows if r.status == "sent"),
        "sent_this_month": sent_month,
        "queried": sum(1 for r in rows if r.status == "queried"),
        "agreed": sum(1 for r in rows if r.status == "agreed"),
        "overdue": sum(1 for r in rows if r.status == "draft"
                       and (due_date(r.period) or today) < today),
    }


# =============================================================================
#  The setup — how a treaty's statements are made and sent
# =============================================================================

def _setup_json(s, c: Contract, su: Optional[CededSetup], p: Principal) -> dict:
    tr = _tr()
    st = tr.load_structure(s, c.id, names=False) or {}
    protected = tr.programmes(s, c)
    panel: list[int] = []
    for lay in st.get("layers") or []:
        for sh in lay.get("shares") or []:
            pid = sh.get("reinsurer_party_id")
            if pid and pid not in panel:
                panel.append(pid)
    parties = {x.id: x.legal_name for x in s.query(Party).filter(
        Party.id.in_(panel or [0]))}
    contacts: dict[int, list] = {}
    for ct in s.query(PartyContact).filter(PartyContact.party_id.in_(panel or [0])):
        contacts.setdefault(ct.party_id, []).append(
            {"id": ct.id, "name": ct.full_name, "email": ct.email})
    heads = {k: (h, kind) for k, h, kind in ce.LINE_COLUMNS}
    return {
        "contract_id": c.id, "treaty": c.name or f"Treaty {c.id}",
        "type_label": tt.label(st.get("treaty_type")),
        "has_structure": bool(st),
        "setup": None if su is None else {
            "status": su.status, "frequency": su.frequency, "due_days": su.due_days,
            "program_ids": su.program_ids, "columns": su.columns,
            "layout": su.layout or {"source": "kavachio"},
            "input_layout": su.input_layout or {"source": "brokers"},
            "rules": rules_for(s, c, st),
            "has_layout_file": su.layout_blob is not None,
            "file_format": su.file_format, "recipients": su.recipients or {},
            "cc": su.cc or [], "send_mode": su.send_mode, "note": su.note,
            "submitted_at": _iso(su.submitted_at), "approved_at": _iso(su.approved_at),
            "updated_at": _iso(su.updated_at)},
        "defaults": {"frequency": "monthly", "due_days": DUE_DAYS,
                     "program_ids": None,
                     "columns": ce.default_columns(st.get("treaty_type")),
                     "layout": {"source": "kavachio"},
                     "input_layout": {"source": "brokers"},
                     "rules": trr.derive(st, c.inception_dt, c.expiry_dt, c.commercial_terms) if st else [],
                     "file_format": "xlsx", "recipients": {}, "cc": [],
                     "send_mode": "manual"},
        "options": {
            "programmes": protected,
            "reinsurers": [{"party_id": pid, "name": parties.get(pid),
                            "contacts": contacts.get(pid, [])} for pid in panel],
            "columns": [{"key": k, "label": h, "kind": kind}
                        for k, (h, kind) in heads.items()],
            # What a carrier layout's column can be filled from, beyond the
            # treaty's own figures: the fields the programmes' bordereaux carry.
            "fields": [{"key": f"{cl.FIELD}{f}", "label": cl.label(f"{cl.FIELD}{f}")}
                       for f in sorted(_available(s, c)[0])],
            "share": {"key": cl.SHARE[0], "label": cl.SHARE[1]},
            "context": [{"key": k, "label": h} for k, h in cl.CONTEXT],
            # The input templates, and how each worked-out figure reads them.
            "inputs": _inputs(s, c),
            # Every data-model field an input column of the carrier's own file
            # can be tied to.
            "all_fields": _all_fields(),
            # Which fields each treaty rule reads — so its input column can be named.
            "rule_fields": {k: list(v) for k, v in trr.SUBJECT_FIELDS.items()},
            "derived": {k: {"how": how, "fields": list(fields)}
                        for k, (how, fields) in cl.DERIVED.items()},
            "frequencies": list(ce.FREQUENCIES),
        },
        "carrier_admin": _cr()._speaks_for_carrier(p),
    }


def _available(s, c: Contract) -> tuple[set[str], dict[str, str]]:
    """The data-model fields the bordereaux feeding this treaty carry — from
    the output templates of its programmes — and their headings."""
    own = own_layout(setup_of(s, c))
    if own:
        cols = [cc for cc in own.get("columns") or [] if cc.get("field")]
        return ({cc["field"] for cc in cols},
                {cl._norm(cc.get("name")): cc["field"] for cc in cols},
                {cl._norm(cc.get("name")): cc.get("name") for cc in own.get("columns") or []
                 if cc.get("name")})
    ids = _protected_ids(s, c)
    if not ids:
        return set(), {}, {}
    # The programmes' templates, and the ones their live files were written to.
    used = {o.template_id for o, _v in _live_exports(s, c.tenant_id, ids) if o.template_id}
    tmpls = (s.query(ExportTemplate)
             .filter(ExportTemplate.tenant_id == c.tenant_id,
                     or_(ExportTemplate.program_id.in_(ids),
                         ExportTemplate.id.in_(used or {0})))
             .order_by(ExportTemplate.is_active.desc(), ExportTemplate.id.desc())
             .limit(40).all())
    return cl.source_columns([t.structure or {} for t in tmpls])


def _inputs(s, c: Contract) -> list[dict]:
    """The INPUT side of the setup: for each programme feeding the treaty, the
    template its bordereaux are written to — every column, and the data-model
    field it is tied to. Or, when the setup reads a file of the carrier's own,
    that file's columns. What
    the reinsurance bordereau is read from."""
    own = own_layout(setup_of(s, c))
    if own:
        return [{"template_id": 0, "name": own.get("filename") or "Your file",
                 "version": None, "active": True, "program_id": None,
                 "programme": "Your file", "own": True,
                 "files": len(_own_inputs(s, c)),
                 "columns": [{"name": cc.get("name"), "field": cc.get("field"),
                              "sheet": own.get("sheet_name")}
                             for cc in own.get("columns") or []]}]
    ids = _protected_ids(s, c)
    if not ids:
        return []
    names = {p.id: p.name for p in s.query(Program).filter(Program.id.in_(ids))}
    live = {}
    for o, _v in _live_exports(s, c.tenant_id, ids):
        if o.template_id:
            live[o.template_id] = live.get(o.template_id, 0) + 1
    tmpls = (s.query(ExportTemplate)
             .filter(ExportTemplate.tenant_id == c.tenant_id,
                     or_(ExportTemplate.program_id.in_(ids),
                         ExportTemplate.id.in_(set(live) or {0})))
             .order_by(ExportTemplate.is_active.desc(), ExportTemplate.id.desc())
             .limit(20).all())
    out = []
    for t in tmpls:
        cols = []
        for sh in (t.structure or {}).get("sheets") or []:
            for col in sh.get("columns") or []:
                if col.get("column_name"):
                    cols.append({"name": str(col["column_name"]),
                                 "field": col.get("canonical_field"),
                                 "sheet": sh.get("sheet_name")})
        out.append({"template_id": t.id, "name": t.name,
                    "version": t.version, "active": bool(t.is_active),
                    "program_id": t.program_id,
                    "programme": names.get(t.program_id),
                    "files": live.get(t.id, 0), "columns": cols})
    return out


def _all_fields() -> list[dict]:
    from data_model import DATA_MODEL
    return [{"key": k, "label": cl.label(f"{cl.FIELD}{k}")}
            for k, v in sorted(DATA_MODEL.items()) if v.get("source") != "system"]


@router.post("/reinsurance/setups/{contract_id}/input/upload")
async def input_sample_upload(contract_id: int, file: UploadFile = File(...),
                              p: Principal = Depends(current_principal)):
    """A sample of the carrier's own input file: its columns, each tied to a
    data-model field by the same AI pass a broker's sample gets. Saves
    nothing — it is saved with the setup."""
    cr = _cr()
    cr._require_carrier(p)
    data = await file.read()
    filename = file.filename or "input.xlsx"
    with SessionLocal() as s:
        cr._contract_access(s, p, contract_id)
    try:
        structure = await run_in_threadpool(cl.parse_upload, data, filename)
    except ValueError as e:
        raise HTTPException(400, str(e))
    await run_in_threadpool(cl.map_structure, structure)
    return cl.input_proposal(structure, filename)


@router.post("/reinsurance/bordereaux/input")
async def upload_period_input(contract_id: int = Form(...), period: str = Form(...),
                              file: UploadFile = File(...),
                              p: Principal = Depends(current_principal)):
    """This period's file of the carrier's own, for a treaty whose setup reads
    from one — stored as the next version for the period, and the statement
    prepared (or refreshed) from it."""
    cr = _cr()
    cr._require_carrier(p)
    data = await file.read()
    period = (period or "").strip()
    layout = await _file_layout(p, contract_id, data, file.filename or "")
    with SessionLocal() as s:
        c = cr._contract_access(s, p, contract_id)
        su = active_setup(s, c)
        if su is None:
            raise HTTPException(409, "this treaty has no active setup")
        if ce.period_bounds(period) is None or ce.bucket(period, _frequency(su)) != period:
            raise HTTPException(400, {"message": "That is not one of this setup's periods.",
                                      "errors": {"period": "e.g. 2027-03, 2027-Q1 or 2027"}})
        if len(data) > cl.MAX_BYTES * 4:
            raise HTTPException(400, "the file is larger than 20 MB")
        try:
            rows = cl.read_input(layout, data, file.filename or "")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"the file could not be read ({e})")
        if not rows:
            raise HTTPException(400, "no rows were read from the file — check it "
                                     "has the same columns as the sample")
        last = (s.query(CededInput).filter(CededInput.contract_id == c.id,
                                           CededInput.period == period)
                .order_by(CededInput.version_no.desc()).first())
        s.add(CededInput(tenant_id=c.tenant_id, contract_id=c.id, period=period,
                         version_no=(last.version_no + 1) if last else 1,
                         filename=file.filename, blob=data, rows=len(rows),
                         layout=layout, uploaded_by_user_id=p.user_id))
        s.flush()
        cb = build(s, c, period, p.user_id)
        s.commit()
        cb_id = cb.id
    return detail(cb_id, p)


async def _file_layout(p: Principal, contract_id: int, data: bytes,
                       filename: str) -> dict:
    """How to read an uploaded period file: the setup's own input layout when
    it has one; otherwise matched from this file's headings — first to the
    brokers' column headings on the treaty's programmes (a carrier's file
    often uses the same), then by the AI pass a broker's sample gets."""
    with SessionLocal() as s:
        c = _cr()._contract_access(s, p, contract_id)
        own = own_layout(setup_of(s, c))
        if own:
            return own
        _fields, by_heading, _names = _available(s, c)
    try:
        structure = await run_in_threadpool(cl.parse_upload, data, filename)
    except ValueError as e:
        raise HTTPException(400, str(e))
    await run_in_threadpool(cl.map_structure, structure)
    lay = cl.input_proposal(structure, filename)
    for col in lay["columns"]:
        hit = by_heading.get(cl._norm(col["name"]))
        if hit:
            col["field"] = hit
    if not any(col["field"] for col in lay["columns"]):
        raise HTTPException(400, "none of the file's columns could be matched — "
                                 "check it has a heading row")
    return lay


def _treaty_text(s, c: Contract) -> tuple[str, Optional[str]]:
    """The treaty's wording as text, and where it came from: the signed copy
    or the wording attached, else the wording written in Kavachio."""
    cr = _cr()
    from db import ContractDocument
    docs = (s.query(ContractDocument)
            .filter(ContractDocument.contract_id == c.id,
                    ContractDocument.kind == "contract",
                    ContractDocument.is_active.is_(True))
            .order_by(ContractDocument.is_executed_copy.desc(),
                      ContractDocument.created_at.desc()).all())
    for d in docs:
        data = cr._doc_bytes(d)
        if data:
            try:
                text = cr._parse_to_text(data, d.filename or "treaty.pdf")
            except Exception as e:  # noqa: BLE001
                log.warning("treaty %s: could not read %s: %s", c.id, d.filename, e)
                continue
            if (text or "").strip():
                return text, d.filename
    secs = ((c.wording_sections or {}).get("sections")
            if isinstance(c.wording_sections, dict) else None) or []
    text = "\n\n".join(f"{x.get('title') or ''}\n{x.get('body') or ''}" for x in secs)
    return text, ("the wording written in Kavachio" if text.strip() else None)


@router.post("/reinsurance/setups/{contract_id}/rules/extract")
async def extract_treaty_rules(contract_id: int,
                               p: Principal = Depends(current_principal)):
    """Read the treaty's wording for the conditions the ceded business must
    meet, and return the rules with them added — the terms rules as they
    stand, plus these. Saves nothing; they are saved with the setup."""
    cr = _cr()
    cr._require_carrier(p)
    with SessionLocal() as s:
        c = cr._contract_access(s, p, contract_id)
        text, source = _treaty_text(s, c)
        current = rules_for(s, c)
    if not source:
        raise HTTPException(409, "This treaty has no wording to read — attach the "
                                 "signed treaty or write its wording first")
    try:
        found = await run_in_threadpool(trr.extract, text)
    except Exception as e:  # noqa: BLE001
        log.warning("treaty rules extract failed: %s", e)
        raise HTTPException(502, "The wording could not be read just now. Try again.")
    rules = [r for r in current if r.get("source") != "wording"] + found
    return {"rules": rules, "read_from": source, "found": len(found)}


def _cell(v):
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def _set_layout_file(su: CededSetup, layout: dict, b64: Optional[str]) -> None:
    """Keep the carrier's file with the setup. A save without one keeps the
    file already held; Kavachio's own layout needs none."""
    import base64
    if (layout or {}).get("source") in (None, "kavachio"):
        su.layout_blob = None
        return
    if b64:
        try:
            data = base64.b64decode(b64, validate=True)
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "the layout file did not arrive whole — upload it again")
        if len(data) > cl.MAX_BYTES:
            raise HTTPException(400, "the layout file is larger than 5 MB")
        su.layout_blob = data
    elif layout.get("source") == "uploaded" and su.layout_blob is None:
        raise HTTPException(400, {"message": "Upload your file again.",
                                  "errors": {"layout": "Upload your file again."}})


def _proposal(s, c: Contract, structure: dict, *, source: str, data: bytes,
              filename: str, standard: Optional[dict] = None) -> dict:
    import base64
    st = _tr().load_structure(s, c.id, names=False) or {}
    available, by_heading, names = _available(s, c)
    layout, cols = cl.propose(structure, source=source, available=available,
                              by_heading=by_heading, treaty_type=st.get("treaty_type"),
                              names=names)
    layout["filename"] = filename
    if standard:
        layout["standard"] = standard
    return {"layout": layout, "columns": cols,
            "layout_file": base64.b64encode(data).decode(),
            "file_format": "csv" if filename.lower().endswith(".csv") else "xlsx",
            "unfilled": [x["heading"] for x in cols
                         if x["keep"] and not cl.fillable(x["key"], available,
                                                          set(names.values()))]}


@router.post("/reinsurance/setups/{contract_id}/layout/upload")
async def layout_upload(contract_id: int, file: UploadFile = File(...),
                        p: Principal = Depends(current_principal)):
    """Read the carrier's own Excel or CSV and say what each of its columns
    will be filled from. Saves nothing — the setup screen shows the proposal
    and the file is saved with the setup."""
    cr = _cr()
    cr._require_carrier(p)
    data = await file.read()
    filename = file.filename or "layout.xlsx"
    with SessionLocal() as s:
        cr._contract_access(s, p, contract_id)
    try:
        structure = await run_in_threadpool(cl.parse_upload, data, filename)
    except ValueError as e:
        raise HTTPException(400, str(e))
    await run_in_threadpool(cl.map_structure, structure)
    with SessionLocal() as s:
        c = cr._contract_access(s, p, contract_id)
        return _proposal(s, c, structure, source="uploaded", data=data,
                         filename=filename)


class StandardIn(BaseModel):
    standard_id: Optional[str] = None
    jurisdiction: Optional[str] = None


@router.post("/reinsurance/setups/{contract_id}/layout/standard")
async def layout_standard(contract_id: int, body: StandardIn,
                          p: Principal = Depends(current_principal)):
    """Build the layout from the Lloyd's risk sheet, the way a broker's output
    template is built from it: the territory's published columns, mandatory
    ones and those the bordereaux can fill ticked, then the treaty's
    reinsurance columns."""
    import reporting_standards as standards
    import output_template_fields as otf
    cr = _cr()
    cr._require_carrier(p)
    std = standards.get(body.standard_id)
    if not std:
        raise HTTPException(400, "no reporting standard is bundled with this service")
    with SessionLocal() as s:
        cr._contract_access(s, p, contract_id)
    try:
        blob, jurisdiction = await run_in_threadpool(
            standards.sheet_bytes, std["id"], body.jurisdiction)
    except KeyError:
        raise HTTPException(400, f"'{body.jurisdiction}' is not a layout in this standard")
    from exporter import parse_template
    structure = await run_in_threadpool(parse_template, blob, filename="standard.xlsx")
    await run_in_threadpool(cl.map_structure, structure)
    otf.apply_standard(structure, standards.fields(std["id"], jurisdiction))
    with SessionLocal() as s:
        c = cr._contract_access(s, p, contract_id)
        safe = "".join(ch if ch.isalnum() else "_" for ch in jurisdiction)
        return _proposal(s, c, structure, source="standard", data=blob,
                         filename=f"lloyds_risk_{safe}.xlsx",
                         standard={"standard_id": std["id"], "label": std["label"],
                                   "version": std["version"],
                                   "jurisdiction": jurisdiction})


@router.get("/reinsurance/setups")
def list_setups(p: Principal = Depends(current_principal)):
    """Every live treaty and where its reinsurance bordereau setup stands."""
    cr = _cr()
    cr._require_carrier(p)
    tr = _tr()
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, p)
        out = []
        for c in (s.query(Contract).filter(Contract.tenant_id == tid,
                                           Contract.contract_type == tr.REINSURER)
                  .order_by(Contract.name)):
            state = tr._state(c)
            if state in tr.CLOSED_STATES:
                continue
            su = setup_of(s, c)
            st = tr.load_structure(s, c.id, names=False)
            out.append({
                "contract_id": c.id, "treaty": c.name or f"Treaty {c.id}",
                "type_label": tt.label((st or {}).get("treaty_type")),
                "lifecycle": state, "has_structure": bool(st),
                "programmes": len(_protected_ids(s, c)),
                "status": su.status if su is not None else None,
                "frequency": su.frequency if su is not None else None,
                "send_mode": su.send_mode if su is not None else None,
                "updated_at": _iso(su.updated_at) if su is not None else None,
            })
        return {"items": out}


@router.get("/reinsurance/setups/{contract_id}")
def get_setup(contract_id: int, p: Principal = Depends(current_principal)):
    cr = _cr()
    cr._require_carrier(p)
    with SessionLocal() as s:
        c = cr._contract_access(s, p, contract_id)
        if (c.contract_type or "") != _tr().REINSURER:
            raise HTTPException(404, "not a reinsurance treaty")
        return _setup_json(s, c, setup_of(s, c), p)


class SetupIn(BaseModel):
    frequency: str = "monthly"
    due_days: int = DUE_DAYS
    program_ids: Optional[list[int]] = None
    columns: list[dict] = []
    # {"source": kavachio | uploaded | standard, ...} — see ceded_layout.
    layout: Optional[dict] = None
    # {"source": "brokers"} or the carrier's own input file — CededSetup.
    input_layout: Optional[dict] = None
    # The treaty's rules as set on the screen (treaty_rules); None keeps them.
    rules: Optional[list] = None
    # The uploaded file (or Lloyd's sheet), base64, as the layout endpoints
    # returned it — sent once, when the layout is first saved.
    layout_file: Optional[str] = None
    file_format: str = "xlsx"
    recipients: dict = {}
    cc: list[str] = []
    send_mode: str = "manual"


def _clean_setup(s, c: Contract, body: SetupIn) -> dict:
    import re as _re
    errors: dict[str, str] = {}
    if body.frequency not in ce.FREQUENCIES:
        errors["frequency"] = "Choose monthly, quarterly or annually."
    if not 0 <= int(body.due_days) <= 365:
        errors["due_days"] = "Between 0 and 365 days."
    protected = {x["id"] for x in _tr().programmes(s, c)}
    progs = None
    if body.program_ids is not None:
        progs = [int(x) for x in body.program_ids]
        if not progs:
            errors["program_ids"] = "Choose at least one programme."
        elif any(x not in protected for x in progs):
            errors["program_ids"] = "Only programmes this treaty protects."
    layout = dict(body.layout or {"source": "kavachio"})
    source = layout.get("source") or "kavachio"
    if source not in cl.SOURCES:
        errors["layout"] = "Choose Kavachio's layout, your own file or the Lloyd's risk sheet."
        source = "kavachio"
    if source == "kavachio":
        known = {k for k, _h, _kind in ce.LINE_COLUMNS}
        cols = [{"key": col.get("key"),
                 "heading": (str(col.get("heading") or "").strip()
                             or next(h for k, h, _ in ce.LINE_COLUMNS if k == col.get("key")))}
                for col in body.columns if col.get("key") in known]
        layout = {"source": "kavachio"}
    else:
        cols = []
        for col in body.columns:
            key = str(col.get("key") or "")
            if key and key not in cl.LINE_KEYS and not (
                    key.startswith(cl.FIELD) and _re.fullmatch(r"[a-z0-9_]+", key[len(cl.FIELD):])) \
                    and not (key.startswith(cl.COL) and 0 < len(key) - len(cl.COL) <= 200):
                continue
            heading = str(col.get("heading") or "").strip()
            if not heading:
                errors["columns"] = "Every column needs a heading."
            idx = col.get("column_index")
            cols.append({"key": key, "heading": heading,
                         "column_index": int(idx) if idx is not None else None,
                         "keep": bool(col.get("keep", True)),
                         "required": bool(col.get("required")), "ref": col.get("ref"),
                         **({"added": True} if col.get("added") else {})})
        layout = {k: layout.get(k) for k in ("source", "filename", "sheet_name",
                                            "header_row", "data_start_row", "standard")}
        if source == "uploaded":
            # The file decides the format: an Excel is filled in, a CSV written.
            body.file_format = "csv" if str(layout.get("filename") or "").lower() \
                .endswith(".csv") else "xlsx"
            if layout.get("sheet_name") is None or not cols:
                errors["layout"] = "Upload your file again."
        missing = [c["heading"] for c in cols if c.get("required") and not c["keep"]]
        if missing:
            errors["columns"] = ("The Lloyd's standard makes these mandatory: "
                                 + ", ".join(missing[:5]))
    if not any(c.get("keep", True) and c["key"] for c in cols):
        errors.setdefault("columns", "Choose at least one column.")
    if body.file_format not in ("xlsx", "csv"):
        errors["file_format"] = "Excel or CSV."
    inp = dict(body.input_layout or {"source": "brokers"})
    if inp.get("source") == "own":
        icols = []
        for col in inp.get("columns") or []:
            f = col.get("field") or None
            if f and not _re.fullmatch(r"[a-z0-9_]+", str(f)):
                f = None
            if str(col.get("name") or "").strip():
                icols.append({"name": str(col["name"]).strip(),
                              "column_index": int(col.get("column_index") or 0),
                              "field": f})
        if not any(cc["field"] for cc in icols):
            errors["input_layout"] = ("Upload a sample of your file and tie at least "
                                      "one of its columns to a field.")
        inp = {"source": "own", "filename": inp.get("filename"),
               "sheet_name": inp.get("sheet_name"),
               "header_row": int(inp.get("header_row") or 0),
               "data_start_row": int(inp.get("data_start_row") or 1),
               "columns": icols}
    else:
        inp = {"source": "brokers"}
    st = _tr().load_structure(s, c.id, names=False) or {}
    panel = {sh.get("reinsurer_party_id") for lay in st.get("layers") or []
             for sh in lay.get("shares") or []}
    rec: dict[str, list[int]] = {}
    for pid, ids in (body.recipients or {}).items():
        pid_i = int(pid)
        if pid_i not in panel:
            continue
        ids = [int(i) for i in ids or []]
        mine = {ct.id for ct in s.query(PartyContact).filter(
            PartyContact.party_id == pid_i)}
        if any(i not in mine for i in ids):
            errors[f"recipients.{pid}"] = "That person is not a contact of this reinsurer."
        if ids:
            rec[str(pid_i)] = ids
    cc = [e.strip() for e in body.cc if (e or "").strip()]
    bad = [e for e in cc if not _re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e)]
    if bad:
        errors["cc"] = f"Not an email address: {', '.join(bad)}"
    if body.send_mode not in ("manual", "auto"):
        errors["send_mode"] = "Manual or automatic."
    if errors:
        raise HTTPException(400, {"message": "The setup has something to fix.",
                                  "errors": errors})
    return {"frequency": body.frequency, "due_days": int(body.due_days),
            "program_ids": progs, "columns": cols, "layout": layout,
            "input_layout": inp,
            **({"rules": trr.clean(body.rules)} if body.rules is not None else {}),
            "file_format": body.file_format,
            "recipients": rec, "cc": cc, "send_mode": body.send_mode}


@router.put("/reinsurance/setups/{contract_id}")
def save_setup(contract_id: int, body: SetupIn,
               p: Principal = Depends(current_principal)):
    """Save the setup as a draft. A change to an active setup takes effect once
    it is activated again — the same rule a broker's setup follows."""
    cr = _cr()
    cr._require_carrier(p)
    with SessionLocal() as s:
        c = cr._contract_access(s, p, contract_id)
        if (c.contract_type or "") != _tr().REINSURER:
            raise HTTPException(404, "not a reinsurance treaty")
        vals = _clean_setup(s, c, body)
        su = setup_of(s, c)
        if su is None:
            su = CededSetup(tenant_id=c.tenant_id, contract_id=c.id)
            s.add(su)
        for k, v in vals.items():
            setattr(su, k, v)
        _set_layout_file(su, vals["layout"], body.layout_file)
        su.status = "draft"
        su.note = None
        su.updated_by_user_id = p.user_id
        su.updated_at = _now()
        s.commit()
        return _setup_json(s, c, su, p)


@router.delete("/reinsurance/setups/{contract_id}")
def delete_setup(contract_id: int, p: Principal = Depends(current_principal)):
    """Delete a draft setup — the broker setup's Delete Draft. An active one
    is not deleted: statements are being prepared with it."""
    cr = _cr()
    cr._assert_speaks_for_carrier(p, "delete a reinsurance bordereau setup")
    with SessionLocal() as s:
        c = cr._contract_access(s, p, contract_id)
        su = setup_of(s, c)
        if su is None:
            raise HTTPException(404, "this treaty has no setup")
        if su.status == "active":
            raise HTTPException(409, "this setup is active — statements are being "
                                     "prepared with it, so it is not deleted")
        s.delete(su)
        s.commit()
        try:
            from audit import log_activity
            log_activity(c.tenant_id, cr._principal_email_for(s, p),
                         "reinsurance_setup_deleted", target=f"contract:{c.id}",
                         details={"treaty": c.name}, principal=p)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True}


@router.post("/reinsurance/setups/{contract_id}/activate")
def activate_setup(contract_id: int, p: Principal = Depends(current_principal)):
    """The carrier admin activates; a carrier user sends it to them."""
    cr = _cr()
    cr._require_carrier(p)
    with SessionLocal() as s:
        c = cr._contract_access(s, p, contract_id)
        su = setup_of(s, c)
        if su is None:
            raise HTTPException(409, "save the setup first")
        if not _tr().load_structure(s, c.id, names=False):
            raise HTTPException(409, "the treaty has no structure yet")
        now = _now()
        if cr._speaks_for_carrier(p):
            su.status = "active"
            su.approved_by_user_id = p.user_id
            su.approved_at = now
            action = "reinsurance_setup_activated"
        else:
            su.status = "pending_approval"
            su.submitted_by_user_id = p.user_id
            su.submitted_at = now
            action = "reinsurance_setup_submitted"
        su.note = None
        s.commit()
        try:
            from audit import log_activity
            log_activity(c.tenant_id, cr._principal_email_for(s, p), action,
                         target=f"contract:{c.id}",
                         details={"treaty": c.name}, principal=p)
        except Exception:  # noqa: BLE001
            pass
        return _setup_json(s, c, su, p)


class SendBackIn(BaseModel):
    note: str


@router.post("/reinsurance/setups/{contract_id}/send-back")
def send_back_setup(contract_id: int, body: SendBackIn,
                    p: Principal = Depends(current_principal)):
    cr = _cr()
    cr._assert_speaks_for_carrier(p, "approve a reinsurance bordereau setup")
    with SessionLocal() as s:
        c = cr._contract_access(s, p, contract_id)
        su = setup_of(s, c)
        if su is None or su.status != "pending_approval":
            raise HTTPException(409, "this setup is not waiting for approval")
        if not (body.note or "").strip():
            raise HTTPException(400, "say what needs to change")
        su.status = "draft"
        su.note = body.note.strip()
        s.commit()
        return _setup_json(s, c, su, p)


class PreviewIn(SetupIn):
    period: Optional[str] = None


def _result_payload(s, c: Contract, st: dict, period: str, columns: list) -> dict:
    """A statement worked out for `period` — totals, each reinsurer's share
    and the file's first lines as the reinsurer will see them — without
    saving it. Shared by the setup preview and Check on the process screen."""
    g = gather(s, c, period, st)
    split = ce.by_reinsurer(st, g["result"]["lines"])
    names = {x.id: x.legal_name for x in s.query(Party).filter(
        Party.id.in_(set(split) or {0}))}
    cols = [c_ for c_ in columns if c_.get("keep", True)]
    ctx = cl.context(period=period, treaty=c.name or f"Treaty {c.id}",
                     reinsured=_tr()._carrier_name(s, c),
                     reinsurer=next(iter(names.values()), None),
                     currency=(st.get("terms") or {}).get("currency"))
    return {
        "lines": [{c_["key"]: ln.get(c_["key"]) for c_ in cols}
                  for ln in g["result"]["lines"][:25]],
        # The file as the reinsurer will see it — by position, since a
        # carrier's layout can leave columns blank or repeat a field.
        "table": {"headings": [c_["heading"] for c_ in cols],
                  "rows": [[_cell(cl.value(ln, c_["key"], ctx)) for c_ in cols]
                           for ln in g["result"]["lines"][:25]]},
        "lines_total": len(g["result"]["lines"]),
        "totals": g["result"]["totals"],
        "reinsurers": [{"name": names.get(pid), "shares": part["shares"],
                        "totals": part["totals"]}
                       for pid, part in split.items()],
        "sources": g["src_meta"], "missing": g["missing"],
        "notes": g["notes"], "skipped": len(g["result"]["skipped"]),
        "currency": (st.get("terms") or {}).get("currency"),
        "checks": g.get("checks") or [],
    }


@router.post("/reinsurance/bordereaux/check")
async def check_period(contract_id: int = Form(...), period: str = Form(...),
                       file: Optional[UploadFile] = File(None),
                       p: Principal = Depends(current_principal)):
    """Check: work out a treaty's statement for a period — from this file,
    when its setup reads the carrier's own — and show it, saving nothing.
    The reinsurance counterpart of a broker run's Check."""
    cr = _cr()
    cr._require_carrier(p)
    data = await file.read() if file is not None else None
    period = (period or "").strip()
    layout = (await _file_layout(p, contract_id, data, file.filename or "")
              if data else None)
    with SessionLocal() as s:
        c = cr._contract_access(s, p, contract_id)
        st = _tr().load_structure(s, c.id, names=False)
        su = active_setup(s, c)
        if not st or su is None:
            raise HTTPException(409, "this treaty has no active setup")
        if ce.period_bounds(period) is None or ce.bucket(period, _frequency(su)) != period:
            raise HTTPException(400, {"message": "That is not one of this setup's periods.",
                                      "errors": {"period": "e.g. 2027-03, 2027-Q1 or 2027"}})
        nested = s.begin_nested()
        if layout is not None and data:
            try:
                rows = cl.read_input(layout, data, file.filename or "")
            except Exception as e:  # noqa: BLE001
                nested.rollback()
                raise HTTPException(400, f"the file could not be read ({e})")
            if not rows:
                nested.rollback()
                raise HTTPException(400, "no rows were read from the file — check it "
                                         "has the same columns as your input template")
            last = (s.query(CededInput).filter(CededInput.contract_id == c.id,
                                               CededInput.period == period)
                    .order_by(CededInput.version_no.desc()).first())
            s.add(CededInput(tenant_id=c.tenant_id, contract_id=c.id, period=period,
                             version_no=(last.version_no + 1) if last else 1,
                             filename=file.filename, blob=data, rows=len(rows),
                             layout=layout))
            s.flush()
        out = {"period": period, **_result_payload(s, c, st, period, su.columns or [])}
        nested.rollback()
        return out


@router.post("/reinsurance/setups/{contract_id}/preview")
def preview_setup(contract_id: int, body: PreviewIn,
                  p: Principal = Depends(current_principal)):
    """What a statement would look like with these settings, from the
    bordereaux already received — without saving the settings or anything
    else."""
    cr = _cr()
    cr._require_carrier(p)
    with SessionLocal() as s:
        c = cr._contract_access(s, p, contract_id)
        st = _tr().load_structure(s, c.id, names=False)
        if not st:
            raise HTTPException(409, "the treaty has no structure yet")
        vals = _clean_setup(s, c, body)
        # The settings are applied inside a savepoint that is rolled back: the
        # same code reads them as a real statement would, and none of it stays.
        nested = s.begin_nested()
        su = setup_of(s, c)
        if su is None:
            su = CededSetup(tenant_id=c.tenant_id, contract_id=c.id)
            s.add(su)
        for k, v in vals.items():
            setattr(su, k, v)
        s.flush()
        periods = treaty_periods(s, c.tenant_id, c)
        period = body.period if body.period in periods else (periods[0] if periods else None)
        out: dict = {"periods": periods, "period": period, "lines": [], "totals": {},
                     "reinsurers": [], "sources": [], "missing": [], "notes": [],
                     "skipped": 0}
        if period:
            out.update(_result_payload(s, c, st, period, vals["columns"]))
        nested.rollback()
        return out
