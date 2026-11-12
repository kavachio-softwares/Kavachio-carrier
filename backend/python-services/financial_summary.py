"""
financial_summary.py
────────────────────
Financial Summary & Trends (Phase 2, task 35): written premium, commission,
brokerage, fees, taxes, bordereau net premium and earned premium, by programme
and by broker, for a period against the period before it and the same period a
year earlier.

WHERE THE NUMBERS COME FROM
  The rows of every processed bordereau, read by heading through
  financial_figures.py — the same files Policy Search reads, chosen the same
  way (policy_search._exports: the CURRENT version of each file, a replaced
  contract's month left out, the carrier or broker seat's own files only).
  Policy Search's own reading and premium are not touched.

WHICH PERIOD A FILE IS IN
  The month its submission reports on (exception_tally.export_versions — the
  same period every other screen shows). A file counts in a window only when
  its whole period lies inside it: a quarterly file is in its quarter and its
  year, never in one of its months. A file with no reporting period is in no
  window and is listed as such.

READ WITH THE LATEST MAPPING THE CARRIER SET UP (decision of 6 Nov 2026)
  Every file — old ones included — is read with the latest version of its own
  output template (latest_of), so a mapping the carrier corrects reaches files
  made before the correction. "Its own" is the fork relationship, not the name:
  the field editor's fork (output_template_routes._fork) copies the carrier,
  name, programme, broker, contract, sample workbook and output format and
  takes the next version number, so a version belongs to a file's template
  only when all of those match (a programme/broker/contract left unset on one
  side matches anything: the scope columns were filled in on older rows later).
  Of those versions the newest is the carrier's latest mapping — whether or not
  a Bordereau Setup has been pointed at it yet (a fork does not re-point the
  setups that use the version it was made from). Policy Search and the
  reinsurance statements keep reading each file with the template it was made
  from.

COUNTED ONCE
  An identical file (same stored content) sent twice for the same carrier,
  programme and broker counts once — the newest copy. ITD files reach here
  already reduced to their new rows and adjustments (itd.py).

NOTHING INVENTED
  A total adds only reported and calculated values; how many rows were
  reported, calculated, need review or did not report the figure travels with
  every total, so a missing value is never read as 0. Different currencies are
  never added together: each currency, and "currency not stated", is its own
  group. Net Written Premium is Not available until carriers can confirm
  whether they write assumed reinsurance (decision of 5 Nov 2026).

Every function takes the caller's session; none opens one. Reading never writes.
"""
from __future__ import annotations

import datetime as dt
import re
import threading
from collections import OrderedDict
from typing import Optional

from fastapi import HTTPException

import ceded_engine as ce
import financial_figures as ff
import policy_search as ps

BASES = ("month", "quarter", "year")
WINDOWS = ("current", "previous", "last_year")

# Everything the summary adds up, in the order the screen shows it.
METRICS = ("gross_premium", "total_gwp", "commission_amount", "commission_pct",
           "brokerage_amount", "brokerage_pct", "fees", "taxes", "net_premium",
           "earned_premium")
# A rate's total is the amount over the premium it was earned on.
RATE_OF = {"commission_pct": "commission_amount", "brokerage_pct": "brokerage_amount"}

NOT_STATED, IN_REVIEW = "none", "review"

NET_WRITTEN_PREMIUM = {
    "status": "not_available",
    "formula": "Gross written premium − premium ceded to reinsurance + reinsurance assumed",
    "reason": ("Kavachio does not yet record whether a carrier writes assumed "
               "reinsurance, so this stays Not available until carriers can confirm it."),
}

# Rows remembered across all files; the least recently used files go first.
CACHE_ROWS = 300_000
_LOCK = threading.Lock()
_REMEMBERED: "OrderedDict[int, tuple[str, ff.FileRows]]" = OrderedDict()


# ── periods ─────────────────────────────────────────────────────────────────
_SHAPE = {"month": r"\d{4}-\d{2}", "quarter": r"\d{4}-Q[1-4]", "year": r"\d{4}"}


def label_of(d: dt.date, basis: str) -> str:
    if basis == "month":
        return f"{d.year:04d}-{d.month:02d}"
    if basis == "quarter":
        return f"{d.year:04d}-Q{(d.month - 1) // 3 + 1}"
    return f"{d.year:04d}"


def shift(label: str, basis: str, months: int) -> str:
    """The window `months` away from this one, as a label of the same basis."""
    start = ce.period_bounds(label)[0]
    y, m = divmod(start.year * 12 + start.month - 1 + months, 12)
    return label_of(dt.date(y, m + 1, 1), basis)


def windows(label: str, basis: str) -> dict[str, str]:
    step = {"month": 1, "quarter": 3, "year": 12}[basis]
    return {"current": label, "previous": shift(label, basis, -step),
            "last_year": shift(label, basis, -12)}


def _fits(period: Optional[str], label: str) -> Optional[bool]:
    """True when the file's whole period lies in the window, False when it
    only overlaps it, None when it is not in it at all."""
    fb, wb = ce.period_bounds(period or ""), ce.period_bounds(label)
    if not fb or not wb or fb[1] < wb[0] or fb[0] > wb[1]:
        return None
    return fb[0] >= wb[0] and fb[1] <= wb[1]


def _own_label(period: Optional[str], basis: str) -> Optional[str]:
    """The one window of this basis a file's period fits in, if any."""
    b = ce.period_bounds(period or "")
    if not b:
        return None
    a, z = label_of(b[0], basis), label_of(b[1], basis)
    return a if a == z else None


# ── which template version reads a file ─────────────────────────────────────
# What output_template_routes._fork copies onto the next version, besides the
# carrier (tenant_id) and the name.
_FORK_SCOPE = ("carrier_party_id", "program_id", "broker_party_id", "contract_id")
_FORK_SAME = ("tenant_id", "name", "sample", "output_format")


def _same_lineage(a, b) -> bool:
    return (all(getattr(a, k) == getattr(b, k) for k in _FORK_SAME)
            and all(getattr(a, k) is None or getattr(b, k) is None or getattr(a, k) == getattr(b, k)
                    for k in _FORK_SCOPE))


def latest_of(versions: list) -> dict[int, int]:
    """{template id: id of the version to read its files with} — the newest
    version of its own lineage, never an older one.

    `versions`: every template row that shares a carrier and name with the
    templates asked about — objects with id, version, the _FORK_SAME and
    _FORK_SCOPE attributes (`sample` = the sample workbook's reference or
    hash). Pure, so the rule is testable without a database."""
    out = {}
    for t in versions:
        newer = [v for v in versions
                 if _same_lineage(t, v) and (v.version or 1) >= (t.version or 1)]
        out[t.id] = max(newer, key=lambda v: ((v.version or 1), v.id)).id
    return out


def _latest_templates(s, template_ids: set[int]) -> dict[int, int]:
    """latest_of for these templates, read from the database (nothing the
    structure holds is loaded)."""
    from types import SimpleNamespace
    from sqlalchemy import func, tuple_
    from db import ExportTemplate
    ids = {t for t in template_ids if t is not None}
    if not ids:
        return {}
    pairs = {(tid, name) for tid, name in s.query(ExportTemplate.tenant_id, ExportTemplate.name)
             .filter(ExportTemplate.id.in_(ids))}
    rows = (s.query(ExportTemplate.id, ExportTemplate.version, ExportTemplate.tenant_id,
                    ExportTemplate.name, ExportTemplate.output_format,
                    ExportTemplate.carrier_party_id, ExportTemplate.program_id,
                    ExportTemplate.broker_party_id, ExportTemplate.contract_id,
                    ExportTemplate.template_blob_ref, func.md5(ExportTemplate.template_blob))
            .filter(tuple_(ExportTemplate.tenant_id, ExportTemplate.name).in_(pairs)).all()
            if pairs else [])
    versions = [SimpleNamespace(id=r[0], version=r[1], tenant_id=r[2], name=r[3],
                                output_format=r[4] or "xlsx", carrier_party_id=r[5],
                                program_id=r[6], broker_party_id=r[7], contract_id=r[8],
                                sample=r[9] or r[10]) for r in rows]
    latest = latest_of(versions)
    return {t: latest.get(t, t) for t in ids}


# ── reading ─────────────────────────────────────────────────────────────────
def _bytes(s, o) -> Optional[bytes]:
    import storage
    from db import OutputExport
    blob = None if o.blob_ref else s.query(OutputExport.blob).filter(
        OutputExport.id == o.id).scalar()
    return storage.resolve_bytes(o.blob_ref, blob)


def _size() -> int:
    return sum(len(fr.rows) for _stamp, fr in _REMEMBERED.values())


def _read(s, o, structure: Optional[dict], template_id: int) -> ff.FileRows:
    """One file's rows, read with `template_id` (the latest version of its
    template), remembered until the file or that template changes."""
    if structure is None:
        return ff.FileRows(problem="its output template no longer exists")
    stamp = f"{template_id}:{ff.template_stamp(structure)}:{o.blob_ref or o.blob_md5}"
    with _LOCK:
        hit = _REMEMBERED.get(o.id)
        if hit and hit[0] == stamp:
            _REMEMBERED.move_to_end(o.id)
            return hit[1]
    try:
        fr = ff.read_file(structure, _bytes(s, o), o.filename or "")
    except Exception as e:  # noqa: BLE001 — one bad file must not sink the screen
        fr = ff.FileRows(problem=f"the processed file could not be read ({e})")
    with _LOCK:
        _REMEMBERED[o.id] = (stamp, fr)
        _REMEMBERED.move_to_end(o.id)
        while len(_REMEMBERED) > 1 and _size() > CACHE_ROWS:
            _REMEMBERED.popitem(last=False)
    return fr


def _open_counts(s, export_ids) -> dict[int, int]:
    """Problems still open on each file, counted exactly as Exception Triage
    counts them (exception_tally.export_tally)."""
    from db import OutputExport
    from exception_tally import export_tally
    out = {}
    for eid in export_ids:
        r = s.get(OutputExport, eid)
        if r is not None:
            out[eid] = export_tally(r)["open"]
            s.expunge(r)          # the exceptions column runs to megabytes
    return out


# ── adding up ───────────────────────────────────────────────────────────────
def _blank() -> dict:
    return {k: {"sum": 0.0, "has": False, "num": 0.0, "den": 0.0,
                "counts": {ff.REPORTED: 0, ff.CALCULATED: 0, ff.REVIEW: 0, ff.MISSING: 0}}
            for k in METRICS}


def _add(acc: dict, fig: dict) -> None:
    gross = fig["gross_premium"]
    for k in METRICS:
        f = fig[k]
        a = acc[k]
        a["counts"][f["status"]] += 1
        if k in RATE_OF:
            amt = fig[RATE_OF[k]]
            if amt["value"] is not None and gross["status"] == ff.REPORTED:
                a["num"] += amt["value"]
                a["den"] += gross["value"]
                a["has"] = True
        elif f["value"] is not None:
            a["sum"] += f["value"]
            a["has"] = True


def _close(acc: dict) -> dict:
    out = {}
    for k, a in acc.items():
        if k in RATE_OF:
            value = round(a["num"] / a["den"] * 100, 2) if a["has"] and a["den"] else None
        else:
            value = round(a["sum"], 2) if a["has"] else None
        out[k] = {"value": value, "counts": a["counts"]}
    return out


def _change(cur: Optional[float], prev: Optional[float], rate: bool) -> Optional[float]:
    """% change for an amount; percentage points for a rate. None when there is
    nothing to compare with (no earlier value, or an earlier 0)."""
    if cur is None or prev is None:
        return None
    if rate:
        return round(cur - prev, 2)
    if prev == 0:
        return None
    return round((cur - prev) / abs(prev) * 100, 1)


def _compare(by_window: dict[str, dict]) -> dict:
    """{metric: {current, previous, last_year, change: {previous, last_year}}}."""
    closed = {w: _close(by_window.get(w) or _blank()) for w in WINDOWS}
    out = {}
    for k in METRICS:
        cur = closed["current"][k]["value"]
        out[k] = {
            "unit": "percent" if k in RATE_OF else "money",
            **{w: closed[w][k] for w in WINDOWS},
            "change": {"previous": _change(cur, closed["previous"][k]["value"], k in RATE_OF),
                       "last_year": _change(cur, closed["last_year"][k]["value"], k in RATE_OF)},
        }
    return out


def _currency_key(fig: dict) -> str:
    c = fig["currency"]
    if c["status"] == ff.REPORTED:
        return c["value"]
    return IN_REVIEW if c["status"] == ff.REVIEW else NOT_STATED


def _currency_label(key: str) -> str:
    return {NOT_STATED: "Currency not stated", IN_REVIEW: "Currency needs review"}.get(key, key)


# ── the shared pass ─────────────────────────────────────────────────────────
def _build(s, p, *, basis: str, period: Optional[str], program_id: Optional[int],
           broker_party_id: Optional[int], carrier_id: Optional[int],
           open_problems: str) -> dict:
    """Everything both routes need: the seat, the windows, and every counted
    row of every window with its figures."""
    from exception_tally import export_versions
    if basis not in BASES:
        raise HTTPException(400, f"basis must be one of {', '.join(BASES)}")
    if open_problems not in ("include", "exclude"):
        raise HTTPException(400, "open_problems must be include or exclude")
    if period and not re.fullmatch(_SHAPE[basis], period, re.I):
        raise HTTPException(400, f"period does not match the {basis} basis")
    seat = ps.seat_of(s, p)
    exports = ps._exports(s, seat, carrier_id if seat.kind == "broker" else None)
    names = ps._names(s, exports)
    versions = export_versions(s, [o.id for o in exports]) if exports else {}

    def _choices(key: str, label: str) -> list[dict]:
        seen = {(getattr(o, key), names[label].get(getattr(o, key))) for o in exports
                if getattr(o, key) is not None}
        return sorted(({"id": i, "name": n or f"#{i}"} for i, n in seen),
                      key=lambda c: c["name"].lower())
    filters = {
        "programmes": _choices("program_id", "programme"),
        "brokers": _choices("broker_party_id", "broker") if seat.kind == "carrier" else [],
        "insurers": _choices("tenant_id", "insurer") if seat.kind == "broker" else [],
    }

    scoped = [o for o in exports
              if (program_id is None or o.program_id == program_id)
              and (seat.kind != "carrier" or broker_party_id is None
                   or o.broker_party_id == broker_party_id)]

    # The same stored content twice for one carrier, programme and broker is
    # one file: the newest copy counts.
    def _content(o) -> Optional[tuple]:
        key = o.blob_md5 or o.blob_ref
        return (o.tenant_id, o.program_id, o.broker_party_id, key) if key else None
    newest: dict[tuple, int] = {}
    for o in scoped:
        if (c := _content(o)) is not None:
            newest[c] = max(newest.get(c, 0), o.id)
    copy_of = {}
    for o in scoped:
        c = _content(o)
        if c is not None and newest[c] != o.id:
            copy_of[o.id] = newest[c]

    periods = sorted({lbl for o in scoped if o.id not in copy_of
                      and (lbl := _own_label(ps._period(o, versions), basis))}, reverse=True)
    label = period.upper() if period else (periods[0] if periods else None)
    wins = windows(label, basis) if label else {}

    no_period = [o for o in scoped if not ce.period_bounds(ps._period(o, versions) or "")]
    placed: dict[str, list] = {w: [] for w in WINDOWS}
    longer, copies = [], []
    for o in scoped:
        per = ps._period(o, versions)
        for w, lbl in wins.items():
            fit = _fits(per, lbl)
            if fit is None:
                continue
            if not fit:
                if w == "current":
                    longer.append(o)
                continue
            if o.id in copy_of:
                if w == "current":
                    copies.append(o)
                continue
            placed[w].append(o)

    window_ids = {o.id for w in WINDOWS for o in placed[w]}
    open_n = _open_counts(s, sorted(window_ids)) if window_ids else {}
    if open_problems == "exclude":
        placed = {w: [o for o in ex if not open_n.get(o.id)] for w, ex in placed.items()}

    latest = _latest_templates(s, {o.template_id for w in WINDOWS for o in placed[w]})

    def reading(o) -> int:
        return latest.get(o.template_id, o.template_id)
    structures = ps._structures(s, {reading(o) for w in WINDOWS for o in placed[w]})
    reads: dict[int, ff.FileRows] = {}
    for w in WINDOWS:
        for o in placed[w]:
            if o.id not in reads:
                reads[o.id] = _read(s, o, structures.get(reading(o)), reading(o))

    by_template: dict = {}
    for w in WINDOWS:
        for o in placed[w]:
            by_template.setdefault(reading(o), {})[o.id] = reads[o.id].evidence
    conventions = {}
    for tid, evs in by_template.items():
        pooled = ff.pool(list(evs.values()))
        conventions[tid] = {name: ff.convention(pooled.get(name)) for name in ff.RATES}

    rows: dict[str, list[tuple]] = {w: [] for w in WINDOWS}
    for w in WINDOWS:
        for o in placed[w]:
            for cells in reads[o.id].rows:
                rows[w].append((o, cells, ff.figures(cells, conventions.get(reading(o), {}))))

    return {"seat": seat, "basis": basis, "label": label, "wins": wins,
            "periods": periods, "filters": filters, "names": names, "versions": versions,
            "scoped": scoped, "placed": placed, "reads": reads, "rows": rows,
            "no_period": no_period, "longer": longer, "copies": copies,
            "copy_of": copy_of, "open_n": open_n, "open_problems": open_problems,
            "conventions": conventions, "reading": reading}


# ── the summary ─────────────────────────────────────────────────────────────
def summary(s, p, *, basis: str = "month", period: Optional[str] = None,
            program_id: Optional[int] = None, broker_party_id: Optional[int] = None,
            carrier_id: Optional[int] = None, open_problems: str = "include") -> dict:
    """Totals per currency — overall, by programme and by broker — for the
    period, the period before it and the same period last year."""
    b = _build(s, p, basis=basis, period=period, program_id=program_id,
               broker_party_id=broker_party_id, carrier_id=carrier_id,
               open_problems=open_problems)
    names, rows = b["names"], b["rows"]

    # currency → {"all" | ("programme", id) | ("broker", id): {window: acc}}
    groups: dict[str, dict] = {}
    for w in WINDOWS:
        for o, _cells, fig in rows[w]:
            g = groups.setdefault(_currency_key(fig), {"rows": 0, "acc": {}})
            if w == "current":
                g["rows"] += 1
            for part in ("all", ("programme", o.program_id), ("broker", o.broker_party_id)):
                acc = g["acc"].setdefault(part, {}).setdefault(w, _blank())
                _add(acc, fig)

    def _parts(g: dict, kind: str) -> list[dict]:
        out = []
        for part, by_window in g["acc"].items():
            if part == "all" or part[0] != kind:
                continue
            pid = part[1]
            out.append({"id": pid,
                        "name": names[kind].get(pid) or ("No broker" if kind == "broker" and pid is None else f"#{pid}"),
                        "totals": _compare(by_window)})
        out.sort(key=lambda r: -(r["totals"]["gross_premium"]["current"]["value"] or 0))
        return out

    out_groups = []
    for key, g in sorted(groups.items(), key=lambda kv: (kv[0] in (NOT_STATED, IN_REVIEW), -kv[1]["rows"], kv[0])):
        out_groups.append({
            "currency": key, "label": _currency_label(key), "rows": g["rows"],
            "totals": _compare(g["acc"].get("all") or {}),
            "by_programme": _parts(g, "programme"),
            "by_broker": _parts(g, "broker") if b["seat"].kind == "carrier" else [],
        })

    # The transaction types behind this period's rows, as sent.
    txns: dict[tuple, dict] = {}
    for _o, _cells, fig in rows["current"]:
        t = fig["transaction"]
        k = (t["sent"], t["class"], t["negative"])
        e = txns.setdefault(k, {"sent": t["sent"], "class": t["class"],
                                "negative": t["negative"], "rows": 0})
        e["rows"] += 1
    unknown_pct = sorted({tid for tid, conv in b["conventions"].items()
                          if any(c[0] == "unknown" for c in conv.values())}, key=str)

    reads = b["reads"]
    current = b["placed"]["current"]

    def _file(o, **extra) -> dict:
        return {"export_id": o.id, "filename": o.filename,
                "period": ps._period(o, b["versions"]),
                "programme": names["programme"].get(o.program_id), **extra}

    return {
        "seat": b["seat"].kind,
        "basis": basis,
        "period": b["label"],
        "windows": {w: {"period": lbl, "files": len(b["placed"][w]),
                        "rows": len(rows[w])} for w, lbl in b["wins"].items()},
        "periods": b["periods"],
        "filters": b["filters"],
        "groups": out_groups,
        "net_written_premium": NET_WRITTEN_PREMIUM,
        "transactions": sorted(txns.values(), key=lambda e: -e["rows"]),
        "coverage": {
            "files": len(b["scoped"]),
            "no_period": [_file(o) for o in b["no_period"]],
            "longer_period": [_file(o) for o in b["longer"]],
            "copies": [_file(o, same_as=b["copy_of"].get(o.id)) for o in b["copies"]],
            "unreadable": [_file(o, problem=reads[o.id].problem) for o in current
                           if reads[o.id].problem],
            "open_problems": {"files": sum(1 for o in current if b["open_n"].get(o.id)),
                              "excluded": open_problems == "exclude"},
            "headings_not_in_file": sum(reads[o.id].absent for o in current),
            # Files read with a newer version of their template than they were
            # made from — the carrier has corrected the mapping since.
            "read_with_newer_template": sum(1 for o in current if b["reading"](o) != o.template_id),
            "percent_unverified_templates": len(unknown_pct),
        },
    }


# ── the rows behind it ──────────────────────────────────────────────────────
def rows(s, p, *, basis: str = "month", period: Optional[str] = None,
         window: str = "current", program_id: Optional[int] = None,
         broker_party_id: Optional[int] = None, carrier_id: Optional[int] = None,
         open_problems: str = "include", currency: Optional[str] = None,
         status: Optional[str] = None, page: int = 1, page_size: int = 50) -> dict:
    """One page of the bordereau rows behind a window's totals, each figure
    with its label, the amount as sent, and how a calculated one was worked
    out. `status` keeps the rows where any figure carries that label."""
    if window not in WINDOWS:
        raise HTTPException(400, f"window must be one of {', '.join(WINDOWS)}")
    if status and status not in (ff.REPORTED, ff.CALCULATED, ff.REVIEW, ff.MISSING):
        raise HTTPException(400, "status must be reported, calculated, needs_review or not_reported")
    b = _build(s, p, basis=basis, period=period, program_id=program_id,
               broker_party_id=broker_party_id, carrier_id=carrier_id,
               open_problems=open_problems)
    names = b["names"]
    picked = []
    for o, cells, fig in b["rows"][window]:
        if currency and _currency_key(fig) != currency:
            continue
        if status and not any(fig[k]["status"] == status for k in METRICS):
            continue
        picked.append((o, cells, fig))
    picked.sort(key=lambda t: ((names["programme"].get(t[0].program_id) or "").lower(),
                               t[0].id, t[1]["_row"]))
    start = (page - 1) * page_size
    items = []
    for o, cells, fig in picked[start:start + page_size]:
        items.append({
            "export_id": o.id, "filename": o.filename,
            "template_id": o.template_id, "read_with_template_id": b["reading"](o),
            "period": ps._period(o, b["versions"]),
            "programme": names["programme"].get(o.program_id),
            "broker": names["broker"].get(o.broker_party_id),
            "insurer": names["insurer"].get(o.tenant_id),
            "sheet": cells["_sheet"], "row": cells["_row"],
            "policy": fig["policy"], "transaction": fig["transaction"],
            "cover_start": fig["cover_start"], "cover_end": fig["cover_end"],
            "currency": fig["currency"],
            "figures": {k: fig[k] for k in METRICS},
        })
    return {"items": items, "total": len(picked), "page": page, "page_size": page_size,
            "window": window, "period": b["wins"].get(window)}
