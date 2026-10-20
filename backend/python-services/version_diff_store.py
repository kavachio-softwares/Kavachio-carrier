"""
version_diff_store.py
─────────────────────
Reads what the Previous Submission Reconciliation (version_diff.py) needs —
READ-ONLY: nothing here writes, so the result is information only and every
status, exception, delivery and email stays exactly as it was.

  - which version of its submission a file is, and the LAST ACCEPTED earlier
    version of the same period: the newest earlier version whose status is
    delivered / delivered_flagged (itd_run.DELIVERED — what the carrier
    actually received; there is no approve step for bordereaux yet, D14);
  - both versions' rows: the accepted one as delivered (its landing with the
    fixes saved on it), this one as received;
  - how each version's setup reads its columns (data-model field per input
    column, the columns it maps, the input date format of its date rules,
    the reported-period column of its ITD settings) — the ONLY source for
    which column is which (no heading word lists);
  - fixes recorded on the versions in between (Fix & Validate / secure link),
    so a corrected amount is not called a silent restatement.

The comparison is worked out when the "Previous Submission" tab asks for it
(`screen`), never while a file is processed, and remembered: paging,
filtering and searching the tab reuse it until something it depends on
changes (`_depends_on`).

Every function takes the caller's session; none opens one.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections import OrderedDict
from typing import Optional

import row_key as rk
import version_diff as vd
from itd_run import DELIVERED

log = logging.getLogger("kavachio.version_diff")


def _setting_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "").strip() or default))
    except ValueError:
        return default


def max_rows() -> int:
    """Rows per version above which a file is not compared
    (PREVIOUS_SUBMISSION_MAX_ROWS)."""
    return _setting_int("PREVIOUS_SUBMISSION_MAX_ROWS", 200_000)


def cache_rows() -> int:
    """Rows of worked-out comparisons kept in memory, all files together
    (PREVIOUS_SUBMISSION_CACHE_ROWS); the least recently used go first."""
    return _setting_int("PREVIOUS_SUBMISSION_CACHE_ROWS", 200_000)


# ── which versions ──────────────────────────────────────────────────────────

def _scope_ok(me, base) -> bool:
    """A file can join a thread by quoting its reference (P4), so the accepted
    version must be for the same programme, contract and period. A value
    missing on either side does not count against it."""
    a, b = getattr(me, "arrival", None), getattr(base, "arrival", None)
    if a is None or b is None:
        return True
    for f in ("program_id", "contract_id", "reporting_period"):
        x, y = getattr(a, f, None), getattr(b, f, None)
        if x is not None and y is not None and str(x) != str(y):
            return False
    return True


def last_accepted_version(th, me):
    """The newest version before `me` that the carrier accepted (delivered),
    with rows to compare. None when there is none."""
    cands = [v for v in th.versions
             if v.no < me.no and v.status in DELIVERED and v.landing_id and _scope_ok(me, v)]
    return cands[-1] if cands else None


def find_versions(s, *, arrival_id: Optional[int] = None, export_id: Optional[int] = None,
                  landing_id: Optional[int] = None):
    """(thread, this version, accepted version) for a file, found from its
    arrival, its export or its landing. (None, None, None) when the file is
    not part of a submission (a self-check, or a run with no broker)."""
    import submission_service as svc
    from db import OutputExport
    from intake_models import FileArrival

    a = s.get(FileArrival, arrival_id) if arrival_id else None
    if a is None and export_id:
        a = (s.query(FileArrival).filter(FileArrival.run_export_id == export_id)
             .order_by(FileArrival.id.desc()).first())
    if a is None and landing_id:
        a = (s.query(FileArrival).filter(FileArrival.run_landing_id == landing_id)
             .order_by(FileArrival.id.desc()).first())
    th = me = None
    if a is not None and a.submission_ref and a.version_no:
        th = svc.load(s, a.submission_ref)
        me = th.version(a.version_no) if th else None
    elif export_id:
        out = s.get(OutputExport, export_id)
        if out is not None and getattr(out, "submission_ref", None):
            th = svc.load(s, out.submission_ref)
            me = th.by_export(export_id) if th else None
    if th is None or me is None:
        return None, None, None
    return th, me, last_accepted_version(th, me)


# ── each version's setup and rows ───────────────────────────────────────────

def _resolve(field) -> Optional[str]:
    if not field:
        return None
    try:
        from data_model import resolve_field
        return resolve_field(str(field)) or str(field)
    except Exception:  # noqa: BLE001
        return str(field)


def setup_for(s, landing_id: int, pipeline_id: Optional[int] = None) -> dict:
    """How the setup that ran this landing reads its columns:
    {canonical_of: {input col: data-model field}, mapped_columns: [input cols]
    or None, period_column: {sheet: col}}."""
    from db import DirectFormat, ExportTemplate, LandingRecord, OutputExport, Pipeline

    rec = s.get(LandingRecord, landing_id)
    if rec is None:
        return {}
    fmt = s.get(DirectFormat, rec.format_id) if rec.format_id else None
    pipe = s.get(Pipeline, pipeline_id) if pipeline_id else None
    if pipe is None and rec.output_export_id:
        exp = s.get(OutputExport, rec.output_export_id)
        if exp is not None and exp.pipeline_id:
            pipe = s.get(Pipeline, exp.pipeline_id)
    if pipe is None and fmt is not None:
        pipe = (s.query(Pipeline).filter(Pipeline.input_format_id == fmt.id)
                .order_by(Pipeline.id.desc()).first())
    tpl_id = (getattr(pipe, "output_template_id", None)
              or (getattr(fmt, "output_template_id", None) if fmt else None))
    tpl = s.get(ExportTemplate, tpl_id) if tpl_id else None
    structure = (tpl.structure if tpl is not None else None) or {}
    if isinstance(structure, str):
        import json
        structure = json.loads(structure or "{}")

    out_field = {}
    for sh in structure.get("sheets") or []:
        for c in sh.get("columns") or []:
            f = _resolve(c.get("canonical_field"))
            if f:
                out_field[(rk.heading(sh.get("sheet_name")), rk.heading(c.get("column_name")))] = f
    canonical_of: dict[str, str] = {}
    mapped: list[str] = []
    date_formats: dict[str, str] = {}
    for out_sheet, rules in ((fmt.column_mapping if fmt else None) or {}).items():
        for out_col, rule in (rules or {}).items():
            if not isinstance(rule, dict):
                continue
            srcs = [rule.get("source")] if rule.get("source") else []
            srcs += [op.get("in") for op in rule.get("operands") or []
                     if isinstance(op, dict) and op.get("in")]
            mapped.extend(x for x in srcs if x not in mapped)
            single = (rule.get("kind") == "copy"
                      or (rule.get("kind") == "transform" and rule.get("op") == "date_reformat"))
            f = out_field.get((rk.heading(out_sheet), rk.heading(out_col)))
            if single and f and rule.get("source"):
                canonical_of.setdefault(rule["source"], f)
            if rule.get("op") == "date_reformat" and rule.get("source") and rule.get("from_fmt"):
                date_formats.setdefault(rule["source"], rule["from_fmt"])

    period = {}
    if pipe is not None:
        try:
            import itd_store
            cfg = itd_store.read_config(s, pipe.id) or {}
            period = {name: sc.get("period_column") for name, sc in (cfg.get("sheets") or {}).items()
                      if sc.get("period_column")}
        except Exception:  # noqa: BLE001 — then the key has no reported period
            period = {}
    return {"canonical_of": canonical_of, "mapped_columns": mapped or None,
            "period_column": period, "date_formats": date_formats,
            "pipeline_id": getattr(pipe, "id", None), "format_id": getattr(fmt, "id", None)}


def landing_rows(s, landing_id: int, *, as_delivered: bool) -> dict:
    """A landing's rows. `as_delivered` overlays the fixes saved on it (the
    raw capture is never changed) — the accepted version as the carrier got
    it. Fixes saved after its delivery would be included too (rare: a
    delivered version cannot be Fix & Validated)."""
    from db import LandingRecord
    rec = s.get(LandingRecord, landing_id)
    data = (rec.data if rec is not None else None) or {}
    if as_delivered and data:
        import direct_routes as dr
        data = dr._apply_landing_corrections(s, landing_id, data)
    return data


def corrections_between(s, th, base, me, setups: dict) -> list[dict]:
    """Fixes recorded on the versions after the accepted one and before this
    one ({sheet, values: {part: cleaned}, column, value}) — what the broker
    was asked to change."""
    from sqlalchemy import text
    out = []
    for v in th.versions:
        if not (base.no < v.no < me.no) or not v.landing_id:
            continue
        rows = s.execute(text(
            "SELECT input_sheet, input_row_index, source_column, new_value "
            "FROM landing_correction WHERE landing_id = :l AND kind = 'fix' "
            "AND input_sheet IS NOT NULL AND new_value IS NOT NULL"),
            {"l": v.landing_id}).mappings().all()
        if not rows:
            continue
        data = landing_rows(s, v.landing_id, as_delivered=False)
        setup = setups.get(v.landing_id) or setup_for(s, v.landing_id)
        for c in rows:
            sheet = ((data.get("sheets") or {}).get(c["input_sheet"])) or {}
            try:
                row = (sheet.get("rows") or [])[c["input_row_index"]]
            except (IndexError, TypeError):
                continue
            pc = (setup.get("period_column") or {}).get(c["input_sheet"])
            cols = rk.resolve_key_columns(sheet.get("columns") or [],
                                          setup.get("canonical_of"), pc)["columns"]
            out.append({"sheet": c["input_sheet"], "column": c["source_column"],
                        "value": c["new_value"],
                        "values": rk.row_values(row, cols, rk.PARTS,
                                                setup.get("date_formats"))})
    return out


def _version_info(v) -> dict:
    if v is None:
        return {}
    import submission_service as svc
    return {"version": v.no, "status": v.status,
            "status_text": svc.STATUS_WORDS.get(v.status, v.status),
            "export_id": v.export_id, "landing_id": v.landing_id,
            "received_at": v.created_at.isoformat() if getattr(v, "created_at", None) else None}


def _prepare(s, *, arrival_id: Optional[int] = None, export_id: Optional[int] = None,
             landing_id: Optional[int] = None, pipeline_id: Optional[int] = None) -> dict:
    """Which versions, and how each setup reads them — the cheap part. `done`
    is the result already when there is nothing to compare."""
    th, me, base = find_versions(s, arrival_id=arrival_id, export_id=export_id,
                                 landing_id=landing_id)
    if th is None:
        out = vd._empty("not_in_submission",
                        "Nothing to compare: this file is not part of a broker submission.")
        out.update(accepted=None, this={}, submission_ref=None)
        return {"done": out}
    cur_landing = landing_id or me.landing_id
    meta = {"submission_ref": th.ref if hasattr(th, "ref") else None,
            "this": {**_version_info(me), "landing_id": cur_landing},
            "accepted": _version_info(base) or None}
    done = None
    if base is None:
        done = vd._empty("no_accepted_version",
                         "Nothing to compare yet: no earlier version of this period "
                         "was sent to the carrier.")
    elif cur_landing and base.landing_id == cur_landing:
        done = vd._empty("same_file", "Nothing to compare: this is the version that was "
                                      "sent to the carrier.")
    if done is not None:
        done.update(meta)
        return {"done": done}
    setups = {base.landing_id: setup_for(s, base.landing_id)}
    if cur_landing:
        setups[cur_landing] = setup_for(s, cur_landing, pipeline_id)
    return {"done": None, "th": th, "me": me, "base": base, "cur_landing": cur_landing,
            "meta": meta, "setups": setups}


def _compare(s, prep: dict, current_data: Optional[dict] = None) -> dict:
    """The comparison itself — the costly part: both versions' rows."""
    th, me, base, cur_landing = prep["th"], prep["me"], prep["base"], prep["cur_landing"]
    setups = prep["setups"]
    cur = current_data if current_data is not None else (
        landing_rows(s, cur_landing, as_delivered=False) if cur_landing else {})
    acc = landing_rows(s, base.landing_id, as_delivered=True)
    fixes = corrections_between(s, th, base, me, setups)
    res = vd.diff_versions(acc, cur, accepted_setup=setups[base.landing_id],
                           current_setup=setups.get(cur_landing) or {},
                           corrections=fixes, max_rows=max_rows())
    res.update(prep["meta"])
    return res


def reconcile(s, *, arrival_id: Optional[int] = None, export_id: Optional[int] = None,
              landing_id: Optional[int] = None, pipeline_id: Optional[int] = None,
              current_data: Optional[dict] = None) -> dict:
    """This file against the last accepted version of its submission. Always
    returns a result; its `state` says when nothing could be compared:
      not_in_submission    the file is not a version of a submission
      no_accepted_version  no earlier version was delivered yet (D1)
      same_file            the accepted version is this very landing
      itd_file / too_large / not_compared / compared  (version_diff)"""
    prep = _prepare(s, arrival_id=arrival_id, export_id=export_id, landing_id=landing_id,
                    pipeline_id=pipeline_id)
    return prep["done"] or _compare(s, prep, current_data)


# ── the tab: worked out once, remembered ────────────────────────────────────

_LOCK = threading.Lock()
_REMEMBERED: "OrderedDict[int, tuple[str, dict]]" = OrderedDict()   # export → (stamp, entry)
_WORKING: dict[int, threading.Lock] = {}


def _depends_on(s, prep: dict) -> str:
    """A stamp of everything the comparison reads: the engine, the two
    versions, the fixes saved on them and on the versions between (only the
    accepted landing's fixes change what it says; the others decide whether a
    restatement was asked for), and how each setup reads its columns. The
    landings' own rows are a capture that never changes."""
    from sqlalchemy import text
    th, me, base = prep["th"], prep["me"], prep["base"]
    landings = sorted({v.landing_id for v in th.versions
                       if v.landing_id and base.no <= v.no < me.no})
    fixes = s.execute(text(
        "SELECT count(*), md5(coalesce(string_agg(id::text || ':' || coalesce(kind, '') || ':' "
        "|| coalesce(input_sheet, '') || ':' || coalesce(input_row_index::text, '') || ':' "
        "|| coalesce(source_column, '') || ':' || coalesce(new_value, ''), ',' ORDER BY id), '')) "
        "FROM landing_correction WHERE landing_id = ANY(:l)"),
        {"l": landings or [0]}).one()
    key = [vd.ENGINE_VERSION, me.no, prep["cur_landing"], base.no, base.landing_id,
           list(fixes), prep["setups"]]
    return hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()


def _size(entry: dict) -> int:
    return sum(len(sh.get("entries") or []) for sh in entry["screen"].get("sheets") or [])


def _remember(export_id: int, stamp: str, entry: dict) -> None:
    with _LOCK:
        _REMEMBERED[export_id] = (stamp, entry)
        _REMEMBERED.move_to_end(export_id)
        budget = cache_rows()
        while len(_REMEMBERED) > 1 and sum(_size(e) for _s, e in _REMEMBERED.values()) > budget:
            _REMEMBERED.popitem(last=False)


def _recall(export_id: int, stamp: str) -> Optional[dict]:
    with _LOCK:
        hit = _REMEMBERED.get(export_id)
        if hit and hit[0] == stamp:
            _REMEMBERED.move_to_end(export_id)
            return hit[1]
    return None


def screen(s, export_id: int) -> dict:
    """What the "Previous Submission" tab shows for one output
    (version_diff_sheet.screen), and what its CSV lists (`result`):
    {"screen", "result"}. Worked out on the first request and remembered
    until something the comparison reads changes; requests for the same
    output while it is being worked out wait for it rather than repeating
    it."""
    import version_diff_sheet as vsh
    prep = _prepare(s, export_id=export_id)
    if prep["done"] is not None:
        return {"screen": vsh.screen(prep["done"]), "result": prep["done"]}
    stamp = _depends_on(s, prep)
    hit = _recall(export_id, stamp)
    if hit is not None:
        return hit
    with _LOCK:
        working = _WORKING.setdefault(export_id, threading.Lock())
    with working:
        hit = _recall(export_id, stamp)
        if hit is not None:
            return hit
        res = _compare(s, prep)
        entry = {"screen": vsh.screen(res),
                 # the CSV's source: the rows that differ, not both files
                 "result": {k: v for k, v in res.items() if k != "view"}}
        _remember(export_id, stamp, entry)
        return entry


def clean_download(data: bytes, filename: Optional[str]) -> bytes:
    """An output as "Download BDX" serves it: the bordereau alone. One made
    before 19 Oct 2026 has a "Previous submission" sheet in it, taken out
    here (the comparison is on screen now); the stored file is not changed."""
    if not data or not str(filename or "").lower().endswith(".xlsx"):
        return data
    import version_diff_sheet as vsh
    return vsh.strip_sheet(data)
