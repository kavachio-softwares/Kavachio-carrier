"""MOPR 40.1 — one shared record of every check run and every error found.

Each half of the system used to keep its own record of its checks, and neither
kept the checks that passed:

  * the DIRECT lane (direct_routes._render_landing) wrote its findings onto the
    processed file, output_exports.exceptions — and a re-generate overwrote them;
  * the CANONICAL lane (/export/generate, /export/validate) wrote
    validation_run / validation_exception, and only when something failed;
  * the FILE-ARRIVAL checks (intake_service.land_file) kept only the reason a
    file was refused.

Every one of them now calls `record_run` with what it already had in hand, and
the run lands in three tables (db.CheckRun / CheckResult / CheckError):

  check_run     one row per run — its status, how many checks passed, failed or
                could not run, how many errors of each severity, and the
                volumes it handled (rows, policies, transaction types: 40.2)
  check_result  every check the run made, passed or not
  check_error   every error found, with when it was FIRST found on that file

Recording never costs the run: `record_run` swallows its own failures and
returns None. It writes in its own transaction unless handed a session (the
arrival checks, which commit with the arrival).

`summary` (below) is the 40.2 / 40.3 report read back from these tables.
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

INTAKE, DIRECT, CANONICAL = "intake", "direct", "canonical"
# What a run was. Only RUN is a file handled; the others are looks at one —
# a re-generate of a file already counted, a broker's self-check before
# sending, a setup preview, a dry run before downloading.
RUN, RERUN, SELF_CHECK, PREVIEW, DRY_RUN, FILE_ARRIVAL = (
    "run", "rerun", "self_check", "preview", "dry_run", "file_arrival")

PASSED, FAILED, NOT_RUN = "passed", "failed", "not_run"

# Entries that are messages ABOUT a run, not errors found in it: "the file was
# not validated" and "these rules could not run". The run's status and its
# not-run checks carry them instead.
_NOTICES = ("not_checked", "not_validated")

# Why a person was involved before the checks even ran.
CORRECTED, RELEASED = "corrected_version", "released_from_hold"

# 40.2 — how the transaction types are reported. The system's own types
# (version_diff.type_family, the same reading the ingester makes) fold into
# the three the plan names, the rest of the policy changes, and the rows the
# system could not classify (a blank, 'UNK', or a code not in its list).
GROUPS = (
    ("new", "New", ("new",)),
    ("renewed", "Renewed", ("renewal",)),
    ("cancelled", "Cancelled", ("cancellation", "flat_cancellation")),
    ("other", "Other changes",
     ("endorsement", "installment", "adjustment", "reinstatement", "audit")),
    ("unclassified", "Not classified", ("unknown", "other")),
)
_GROUP_OF = {t: key for key, _w, types in GROUPS for t in types}

_TXN_HEADING = re.compile(r"transaction.?type|trans.?type|tran.?type|txn.?type", re.I)
_UNKNOWN_CODES = {"", "UNK", "UNKNOWN", "N/A", "NA", "-", "NONE", "NULL"}

# The arrival checks, in the order land_file runs them (intake_service). Two
# of them are not in intake_service.CHECKS: what the channel already refused,
# and whether the file says what it is for.
INTAKE_CHECK_WORDS = {
    "size": "Is it small enough to read?",
    "is_spreadsheet": "Is it a spreadsheet at all?",
    "safe_to_open": "Is it safe to open?",
    "known_sender": "Do we know who sent it?",
    "channel": "Was it sent the right way?",
    "identified": "Does it say which programme and period it is for?",
    "malware": "Does it pass the security scan?",
    "not_duplicate": "Is it the same file we already have?",
    "can_open": "Can we open it?",
    "has_rows": "Does it have any rows in it?",
    "required_columns": "Does it have the columns we need?",
    "live_contract": "Is there a live contract to check it against?",
}


# --- reading the rows a run checked -----------------------------------------

def txn_type(value: Any) -> str:
    """A transaction type as the system reads it at ingest — new, renewal,
    cancellation, … — 'unknown' for a blank or the output's own 'UNK', and
    'other' for a code outside the system's list."""
    s = "" if value is None else str(value).strip()
    if s.upper() in _UNKNOWN_CODES:
        return "unknown"
    try:
        from version_diff import type_family
        return type_family(s)
    except Exception:  # noqa: BLE001 — an unreadable code is unclassified
        return "unknown"


def _field(col: dict) -> Optional[str]:
    f = (col or {}).get("canonical_field")
    if not f:
        return None
    try:
        from data_model import resolve_field
        return resolve_field(str(f)) or str(f)
    except Exception:  # noqa: BLE001
        return str(f)


def _columns_for(sheet: dict) -> tuple[Optional[str], Optional[str]]:
    """(transaction-type column, policy-number column) of one output sheet:
    the column the template maps to the data-model field first, then the one
    whose heading says so."""
    cols = [c for c in (sheet or {}).get("columns") or [] if c.get("column_name")]
    names = [c["column_name"] for c in cols]
    txn = next((c["column_name"] for c in cols
                if _field(c) == "premium_transaction_type"), None)
    pol = next((c["column_name"] for c in cols if _field(c) == "policy_number"), None)
    if txn is None:
        txn = next((n for n in names if _TXN_HEADING.search(str(n))), None)
    if pol is None:
        try:
            from export_results import policy_column
            pol = policy_column(names)
        except Exception:  # noqa: BLE001
            pol = None
    return txn, pol


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def volumes(structure: Optional[dict], blocks: Optional[Iterable[dict]]) -> dict:
    """What a run handled, from the rows it checked ([{sheet, records}], the
    shape both lanes validate): rows, distinct policies, and how many rows
    and policies of each transaction type.

    A row with no policy number on a sheet that has a policy column is a
    total or a note, not a policy — it is counted as a row handled but in no
    type. `policies` is None when no sheet has a policy column at all."""
    sheets = {str(sh.get("sheet_name") or ""): sh
              for sh in ((structure or {}).get("sheets") or [])}
    rows = 0
    pols: set = set()
    has_pol_col = False
    by_type: dict[str, dict] = defaultdict(lambda: {"rows": 0, "policies": set()})
    for b in blocks or []:
        recs = [r for r in (b.get("records") or [])
                if isinstance(r, dict) and not all(_blank(v) for v in r.values())]
        rows += len(recs)
        txn_col, pol_col = _columns_for(sheets.get(str(b.get("sheet") or ""), {}))
        has_pol_col = has_pol_col or pol_col is not None
        for r in recs:
            pn = r.get(pol_col) if pol_col else None
            pn = None if _blank(pn) else str(pn).strip()
            if pol_col and pn is None:
                continue
            t = txn_type(r.get(txn_col)) if txn_col else "unknown"
            by_type[t]["rows"] += 1
            if pn:
                by_type[t]["policies"].add(pn)
                pols.add(pn)
    return {
        "rows": rows,
        "policies": len(pols) if has_pol_col else None,
        "transaction_types": {t: {"rows": v["rows"],
                                  "policies": len(v["policies"]) if has_pol_col else None}
                              for t, v in sorted(by_type.items())},
    }


# --- the checks and errors of one run ----------------------------------------

def _errors(exceptions: Optional[Iterable]) -> list[dict]:
    return [e for e in (exceptions or [])
            if isinstance(e, dict) and e.get("error_class") not in _NOTICES]


def _check_key(e: dict) -> str:
    if e.get("check_key"):
        return str(e["check_key"])
    if e.get("rule_id") is not None:
        return f"rule:{e['rule_id']}"
    if e.get("code") == "type_check":
        return f"type:{e.get('sheet') or ''}:{e.get('column') or e.get('field') or ''}"
    return f"code:{e.get('code') or 'unknown'}"


def _int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _text(v: Any, n: int = 500) -> Optional[str]:
    if v is None:
        return None
    s = str(v)
    return s if len(s) <= n else s[: n - 1] + "…"


def fingerprint(e: dict) -> str:
    """The same problem on the same policy (or cell) — stable across the
    re-runs and corrected versions of one file, where row numbers can move but
    a policy number does not."""
    where = (f"p:{str(e.get('policy_number')).strip()}" if e.get("policy_number")
             else f"r:{e.get('row')}")
    raw = "|".join([_check_key(e), str(e.get("sheet") or ""),
                    str(e.get("field") or e.get("column") or ""), where])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def check_results(rules: Optional[Iterable[dict]], column_types: Optional[dict],
                  dv: Optional[dict], errors: list[dict]) -> list[dict]:
    """Every check a validation run made: each contract rule and each column
    type check, passed, failed (with how many errors) or not run (and why).
    `dv` None means the engine never returned — nothing ran."""
    failed_by = Counter(_check_key(e) for e in errors)
    unprocessable = {u.get("rule_id"): u for u in ((dv or {}).get("unprocessable") or [])}
    partial = {p.get("rule_id"): p for p in ((dv or {}).get("partially_checked") or [])}
    out: list[dict] = []
    seen: set = set()
    for r in rules or []:
        rid = r.get("rule_id")
        key = f"rule:{rid}"
        if rid is None or key in seen:
            continue
        seen.add(key)
        row = {"check_key": key, "kind": "contract_rule", "rule_id": _int(rid),
               "name": _text(r.get("rule_name"), 250), "severity": r.get("severity")}
        if dv is None:
            row.update(outcome=NOT_RUN, error_count=0, message="The checks could not run.")
        elif rid in unprocessable:
            row.update(outcome=NOT_RUN, error_count=0,
                       message=_text(unprocessable[rid].get("message")))
        elif failed_by.get(key):
            row.update(outcome=FAILED, error_count=failed_by[key])
        else:
            row.update(outcome=PASSED, error_count=0,
                       message=_text((partial.get(rid) or {}).get("message")))
        out.append(row)
    for sheet, cols in (column_types or {}).items():
        for col, kind in (cols or {}).items():
            key = f"type:{sheet}:{col}"
            if key in seen:
                continue
            seen.add(key)
            n = failed_by.get(key, 0)
            out.append({"check_key": key, "kind": "type_check", "rule_id": None,
                        "name": _text(f"{col} is a {kind}", 250), "severity": "warning",
                        "outcome": NOT_RUN if dv is None else (FAILED if n else PASSED),
                        "error_count": n,
                        "message": "The checks could not run." if dv is None else None})
    return out


def _chain_key(submission_ref, landing_id, export_id, upload_id, arrival_id) -> Optional[str]:
    if submission_ref:
        return f"sub:{submission_ref}"
    for prefix, v in (("landing", landing_id), ("export", export_id),
                      ("upload", upload_id), ("arrival", arrival_id)):
        if v:
            return f"{prefix}:{v}"
    return None


def _arrival_facts(s, arrival_id: Optional[int], export_id: Optional[int]) -> dict:
    """What the file's arrival says about it: its submission, version, period,
    and whether a person was involved before it was checked."""
    try:
        from intake_models import FileArrival
    except Exception:  # noqa: BLE001
        return {}
    a = s.get(FileArrival, arrival_id) if arrival_id else None
    if a is None and export_id:
        a = (s.query(FileArrival).filter(FileArrival.run_export_id == export_id)
             .order_by(FileArrival.id.desc()).first())
    if a is None:
        return {}
    touch = []
    if (a.version_no or 1) > 1 or (a.matched_by or "") in ("secure_link", "fix_validate"):
        touch.append(CORRECTED)
    if (a.resolution or "") == "released":
        touch.append(RELEASED)
    return {"arrival_id": a.id, "submission_ref": a.submission_ref,
            "version_no": a.version_no, "period": a.reporting_period,
            "human_touch": touch}


def record_run(*, lane: str, kind: str, tenant_id: Optional[int],
               status: Optional[str] = None, status_reason: Optional[str] = None,
               rules: Optional[Iterable[dict]] = None, column_types: Optional[dict] = None,
               dv: Optional[dict] = None, exceptions: Optional[Iterable] = None,
               results: Optional[list[dict]] = None,
               structure: Optional[dict] = None, blocks: Optional[Iterable[dict]] = None,
               rows_in: Optional[int] = None,
               export_id: Optional[int] = None, landing_id: Optional[int] = None,
               arrival_id: Optional[int] = None, upload_id: Optional[int] = None,
               pipeline_id: Optional[int] = None, program_id: Optional[int] = None,
               broker_party_id: Optional[int] = None, contract_id: Optional[int] = None,
               filename: Optional[str] = None, period: Optional[str] = None,
               run_by: Optional[str] = None, run_by_user_id: Optional[int] = None,
               session=None) -> Optional[int]:
    """Write one run to the shared record. Returns its id, or None if it could
    not be written — a run is never refused or undone for its record.

    A validation run passes `rules`, `column_types`, `dv` (run_validation's
    result, None if it never returned) and `exceptions`; the arrival checks
    pass their own `results` instead. `structure` + `blocks` are the rows
    checked, for the volumes."""
    args = {k: v for k, v in locals().items() if k != "session"}
    try:
        if session is not None:
            with session.begin_nested():
                return _write(session, **args)
        from db import SessionLocal
        with SessionLocal() as s:
            run_id = _write(s, **args)
            s.commit()
            return run_id
    except Exception:  # noqa: BLE001 — the record never costs the run
        log.warning("check record: could not record a %s %s run", lane, kind, exc_info=True)
        return None


def _write(s, *, lane, kind, tenant_id, status, status_reason, rules, column_types, dv,
           exceptions, results, structure, blocks, rows_in, export_id, landing_id,
           arrival_id, upload_id, pipeline_id, program_id, broker_party_id, contract_id,
           filename, period, run_by, run_by_user_id) -> int:
    from db import CheckError, CheckResult, CheckRun

    now = datetime.utcnow()
    errors = _errors(exceptions)
    if results is None:
        results = check_results(rules, column_types, dv, errors)
    facts = _arrival_facts(s, arrival_id, export_id) if lane != INTAKE else {}
    arrival_id = arrival_id or facts.get("arrival_id")
    submission_ref = facts.get("submission_ref")
    chain = _chain_key(submission_ref, landing_id, export_id, upload_id, arrival_id)
    vol = volumes(structure, blocks) if blocks is not None else {}
    stats = (dv or {}).get("stats") or {}
    sev = Counter(str(e.get("severity") or "warning").lower() for e in errors)
    outcomes = Counter(r["outcome"] for r in results)

    run = CheckRun(
        tenant_id=tenant_id, lane=lane, kind=kind, status=status,
        status_reason=_text(status_reason, 1000),
        export_id=export_id, landing_id=landing_id, arrival_id=arrival_id,
        upload_id=upload_id, pipeline_id=pipeline_id, program_id=program_id,
        broker_party_id=broker_party_id, contract_id=contract_id,
        filename=_text(filename, 250), period=period or facts.get("period"),
        submission_ref=submission_ref, version_no=facts.get("version_no"),
        chain_key=chain, human_touch=facts.get("human_touch") or [],
        checks_total=len(results), checks_passed=outcomes.get(PASSED, 0),
        checks_failed=outcomes.get(FAILED, 0), checks_not_run=outcomes.get(NOT_RUN, 0),
        errors_total=len(errors), critical_count=sev.get("critical", 0),
        warning_count=sev.get("warning", 0) + sev.get("warn", 0),
        info_count=sev.get("info", 0),
        rows_in=rows_in, rows_out=vol.get("rows"),
        rows_checked=_int(stats.get("rows_validated")),
        rows_with_errors=len({(e.get("sheet"), e.get("row")) for e in errors
                              if e.get("row") is not None}) if blocks is not None else None,
        policies=vol.get("policies"), transaction_types=vol.get("transaction_types"),
        run_by=_text(run_by, 250), run_by_user_id=run_by_user_id, created_at=now)
    s.add(run)
    s.flush()

    for r in results:
        s.add(CheckResult(run_id=run.id, tenant_id=tenant_id, created_at=now,
                          check_key=r["check_key"], kind=r["kind"], rule_id=r.get("rule_id"),
                          name=r.get("name"), severity=r.get("severity"),
                          outcome=r["outcome"], error_count=r.get("error_count") or 0,
                          message=r.get("message")))

    # When each problem was first seen on this file: an earlier run of the same
    # file (a re-generate, a corrected version) that found it already.
    first_seen: dict[str, datetime] = {}
    if errors and chain:
        from sqlalchemy import func
        first_seen = dict(
            s.query(CheckError.fingerprint, func.min(CheckError.first_found_at))
            .filter(CheckError.chain_key == chain, CheckError.run_id != run.id)
            .group_by(CheckError.fingerprint).all())
    for e in errors:
        fp = fingerprint(e)
        s.add(CheckError(
            run_id=run.id, tenant_id=tenant_id, lane=lane, export_id=export_id,
            program_id=program_id, broker_party_id=broker_party_id, chain_key=chain,
            check_key=_check_key(e), rule_id=_int(e.get("rule_id")),
            code=_text(e.get("code"), 250), severity=e.get("severity"),
            sheet=_text(e.get("sheet"), 250), row=_int(e.get("row")),
            field=_text(e.get("field") or e.get("column"), 250),
            policy_number=_text(e.get("policy_number"), 250),
            actual_value=_text(e.get("actual_value")),
            message=_text(e.get("message") or e.get("reason"), 1000),
            fingerprint=fp, first_found_at=first_seen.get(fp) or now, created_at=now))
    s.flush()
    return run.id


def intake_results(order: list[str], failed: Optional[str], reason: Optional[str],
                   outcome: str) -> list[dict]:
    """The arrival checks as results: every check before the one that stopped
    the file passed, that one failed, and the rest never ran."""
    out, stopped = [], False
    for key in order:
        row = {"check_key": f"intake:{key}", "kind": "intake", "rule_id": None,
               "name": INTAKE_CHECK_WORDS.get(key, key),
               "severity": "critical" if outcome == "turned_away" else "warning"}
        if stopped:
            row.update(outcome=NOT_RUN, error_count=0,
                       message="Not reached — an earlier check stopped the file.")
        elif key == failed:
            row.update(outcome=FAILED, error_count=1, message=_text(reason))
            stopped = True
        else:
            row.update(outcome=PASSED, error_count=0)
        out.append(row)
    return out


def intake_errors(stopped_by: Optional[str], reason: Optional[str],
                  outcome: str) -> list[dict]:
    """The one error an arrival can have: the check that stopped the file."""
    if not stopped_by:
        return []
    return [{"check_key": f"intake:{stopped_by}", "code": stopped_by,
             "severity": "critical" if outcome == "turned_away" else "warning",
             "message": reason}]


# --- 40.2 / 40.3: the month read back ----------------------------------------

def month_bounds(month: str) -> tuple[datetime, datetime]:
    """'2026-10' → [1 Oct 2026, 1 Nov 2026)."""
    y, m = (int(x) for x in month.split("-")[:2])
    start = datetime(y, m, 1)
    end = datetime(y + (m == 12), m % 12 + 1, 1)
    return start, end


_SEV_RANK = {"info": 1, "warning": 2, "critical": 3}


def _sev(v: Optional[str]) -> Optional[str]:
    v = str(v or "").strip().lower()
    return "warning" if v == "warn" else (v if v in _SEV_RANK else None)


def _pct(n: int, d: int) -> Optional[float]:
    return round(100.0 * n / d, 1) if d else None


def straight_through(run) -> bool:
    """40.3 — a file that passed every check and needed no human handling: it
    came out clean the first time it was checked, and nobody had to release it
    from a hold or send a corrected version first."""
    return run.status == "clean" and not (run.human_touch or [])


def summary(s, tenant_id: int, start: datetime, end: datetime,
            program_id: Optional[int] = None,
            broker_party_id: Optional[int] = None) -> dict:
    """The data quality and volumes of one period, from the shared record.

    A FILE is one processed file (`kind` run, either lane) checked for the
    first time in the period. Its first run is what counts — the file as it
    arrived, before anyone fixed anything — so a re-generate never adds a file
    or hides the errors it first had. Self-checks, previews and dry runs are
    checks, not files: they count in `checks`, never in the volumes."""
    from db import CheckResult, CheckRun

    q = s.query(CheckRun).filter(CheckRun.tenant_id == tenant_id,
                                 CheckRun.created_at >= start, CheckRun.created_at < end)
    if program_id:
        q = q.filter(CheckRun.program_id == program_id)
    if broker_party_id:
        q = q.filter(CheckRun.broker_party_id == broker_party_id)
    runs = q.order_by(CheckRun.id.asc()).all()

    # One file = its first `run`; files are told apart by their export (a
    # canonical run with no export has none to share, so it stands alone).
    files, seen = [], set()
    for r in runs:
        if r.lane not in (DIRECT, CANONICAL) or r.kind != RUN:
            continue
        key = ("export", r.export_id) if r.export_id else ("run", r.id)
        if key in seen:
            continue
        seen.add(key)
        files.append(r)

    groups = {key: {"key": key, "label": words, "policies": 0, "rows": 0}
              for key, words, _t in GROUPS}
    has_policies = False
    for f in files:
        for t, v in (f.transaction_types or {}).items():
            g = groups[_GROUP_OF.get(t, "unclassified")]
            g["rows"] += int(v.get("rows") or 0)
            if v.get("policies") is not None:
                g["policies"] += int(v["policies"])
                has_policies = True

    rows_handled = sum((f.rows_checked if f.rows_checked is not None else f.rows_out) or 0
                       for f in files)
    rows_with_errors = sum(f.rows_with_errors or 0 for f in files)
    stp = [f for f in files if straight_through(f)]
    corrected = sum(1 for f in files if CORRECTED in (f.human_touch or []))
    validation_runs = [r for r in runs if r.lane in (DIRECT, CANONICAL)]
    arrivals = [r for r in runs if r.lane == INTAKE]

    # Which checks failed most, across every file's first run.
    file_ids = [f.id for f in files]
    top = []
    if file_ids:
        tally: dict[str, dict] = {}
        for cr in (s.query(CheckResult).filter(CheckResult.run_id.in_(file_ids),
                                               CheckResult.outcome == FAILED).all()):
            t = tally.setdefault(cr.check_key, {"check": cr.name or cr.check_key,
                                                "kind": cr.kind, "severity": None,
                                                "files": 0, "errors": 0})
            t["files"] += 1
            t["errors"] += cr.error_count or 0
            # The most serious it was anywhere this month.
            t["severity"] = max((t["severity"], _sev(cr.severity)),
                                key=lambda v: _SEV_RANK.get(v or "", 0))
        top = sorted(tally.values(), key=lambda t: (-t["errors"], -t["files"], t["check"]))[:10]

    def _by(*attrs: str) -> list[dict]:
        rows: dict[Any, dict] = {}
        for f in files:
            k = tuple(getattr(f, a) for a in attrs)
            ids = {"id": k[0]} if len(attrs) == 1 else dict(zip(attrs, k))
            row = rows.setdefault(k, {**ids, "files": 0, "rows": 0, "errors": 0,
                                      "rows_with_errors": 0, "straight_through": 0})
            row["files"] += 1
            row["rows"] += (f.rows_checked if f.rows_checked is not None else f.rows_out) or 0
            row["errors"] += f.errors_total or 0
            row["rows_with_errors"] += f.rows_with_errors or 0
            row["straight_through"] += 1 if straight_through(f) else 0
        for row in rows.values():
            row["straight_through_pct"] = _pct(row["straight_through"], row["files"])
            row["rows_with_errors_pct"] = _pct(min(row["rows_with_errors"], row["rows"]),
                                               row["rows"])
        # Most errors first: the brokers to talk to come to the top.
        return sorted(rows.values(), key=lambda r: (-r["errors"], -r["files"]))

    return {
        "from": start.date().isoformat(), "to": (end - timedelta(days=1)).date().isoformat(),
        "volumes": {
            "files": len(files),
            "brokers": len({f.broker_party_id for f in files if f.broker_party_id}),
            "programmes": len({f.program_id for f in files if f.program_id}),
            "corrected_files": corrected,
            "rows": rows_handled,
            "policies": sum(f.policies or 0 for f in files) if has_policies else None,
            "transaction_types": list(groups.values()),
            "policies_known": has_policies,
        },
        "quality": {
            "check_runs": len(validation_runs),
            "checks_run": sum(r.checks_total or 0 for r in files),
            "checks_passed": sum(r.checks_passed or 0 for r in files),
            "checks_failed": sum(r.checks_failed or 0 for r in files),
            "checks_not_run": sum(r.checks_not_run or 0 for r in files),
            "errors": sum(r.errors_total or 0 for r in files),
            "critical": sum(r.critical_count or 0 for r in files),
            "warning": sum(r.warning_count or 0 for r in files),
            "info": sum(r.info_count or 0 for r in files),
            "files_clean": sum(1 for f in files if f.status == "clean"),
            "files_with_errors": sum(1 for f in files if f.status == "has_exceptions"),
            "files_not_validated": sum(1 for f in files if f.status == "not_validated"),
            "top_failing_checks": top,
            "arrivals": len(arrivals),
            "arrivals_accepted": sum(1 for r in arrivals if r.status == "accepted"),
            "arrivals_held": sum(1 for r in arrivals if r.status == "held"),
            "arrivals_turned_away": sum(1 for r in arrivals if r.status == "turned_away"),
        },
        "no_human_help": {
            "files": len(stp),
            "files_pct": _pct(len(stp), len(files)),
            "rows": max(rows_handled - rows_with_errors, 0),
            "rows_pct": _pct(max(rows_handled - rows_with_errors, 0), rows_handled),
        },
        "by_broker": _by("broker_party_id"),
        "by_programme": _by("program_id"),
        # One row per broker on each programme — the screen's one breakdown.
        "by_broker_programme": _by("broker_party_id", "program_id"),
    }
