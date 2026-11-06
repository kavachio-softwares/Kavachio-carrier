"""MOPR 41 — Exception Resolution Performance.

How fast the problems the checks find get fixed, against the carrier's target,
and the age and owner of every problem still open. Read entirely from records
that already exist — nothing new is written while files are processed:

  FOUND   check_error.first_found_at (MOPR 40, check_record): the first time a
          problem was seen on a file, kept across its re-generates and
          corrected versions (`chain_key` + `fingerprint` name one problem).
  FIXED   whichever comes first of
            * a decision on it — fixed, approved as it is, dismissed or
              rejected (exception_decision_log, who and when), or
            * a later check of the same file that no longer finds it (a
              corrected version, or a re-generate with the fixes applied).
          A later check that ran over nothing (not_validated) fixes nothing.
  OWNER   who has to act: the broker whose file it is; the carrier for a file
          no broker sent.
  TARGET  the carrier's own number of days (quality_setting), 5 until changed.

A problem is one (file chain, fingerprint): the same error on the same policy
in versions 1 and 2 of a file is one problem, found when version 1 was checked.
Problems start with the shared check record, so nothing before it is counted.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median
from typing import Any, Iterable, Optional

DEFAULT_TARGET_DAYS = 5
MAX_TARGET_DAYS = 365

# How a problem was closed.
FIX, APPROVE, DISMISS, REJECT, GONE = "fix", "approve", "dismiss", "reject", "gone"
HOW_WORDS = {
    FIX: "Fixed",
    APPROVE: "Approved as it is",
    DISMISS: "Dismissed",
    REJECT: "Rejected",
    GONE: "Gone in a corrected version",
}

# How old the open problems are, in whole days since found.
AGE_BANDS = (
    ("0_2", "0–2 days", 0, 2),
    ("3_7", "3–7 days", 3, 7),
    ("8_30", "8–30 days", 8, 30),
    ("31_plus", "Over 30 days", 31, None),
)

# Runs that are a file being checked for real. A self-check, a set-up preview
# and a dry run are looks at a file: they neither find nor fix a problem.
_REAL_KINDS = ("run", "rerun")
_CHUNK = 500


def _chunks(xs: list, n: int = _CHUNK) -> Iterable[list]:
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def _days(d: timedelta) -> float:
    return max(d.total_seconds(), 0.0) / 86400.0


def _sev(v: Optional[str]) -> Optional[str]:
    v = str(v or "").strip().lower()
    return "warning" if v == "warn" else (v or None)


# --- the target ---------------------------------------------------------------

def target_days(s, tenant_id: int) -> int:
    from db import QualitySetting
    row = s.query(QualitySetting).filter(QualitySetting.tenant_id == tenant_id).first()
    return int(row.fix_target_days) if row and row.fix_target_days else DEFAULT_TARGET_DAYS


def set_target_days(s, tenant_id: int, days: int,
                    user_id: Optional[int] = None) -> tuple[int, int]:
    """Set the carrier's target. Returns (old, new). Caller commits."""
    from db import QualitySetting
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise ValueError("The target must be a whole number of days.")
    if not 1 <= days <= MAX_TARGET_DAYS:
        raise ValueError(f"The target must be between 1 and {MAX_TARGET_DAYS} days.")
    row = s.query(QualitySetting).filter(QualitySetting.tenant_id == tenant_id).first()
    old = int(row.fix_target_days) if row and row.fix_target_days else DEFAULT_TARGET_DAYS
    if row is None:
        row = QualitySetting(tenant_id=tenant_id)
        s.add(row)
    row.fix_target_days = days
    row.updated_by_user_id = user_id
    row.updated_at = datetime.utcnow()
    s.flush()
    return old, days


# --- every problem, found and (maybe) fixed -------------------------------------

def problems(s, tenant_id: int, *, program_id: Optional[int] = None,
             broker_party_id: Optional[int] = None) -> list[dict]:
    """Every problem on this carrier's files, with when it was found and, if
    it has been, when and how it was fixed."""
    from db import CheckError, CheckResult, CheckRun, ExceptionDecisionLog

    q = (s.query(CheckRun.id, CheckRun.chain_key, CheckRun.status, CheckRun.created_at,
                 CheckRun.filename, CheckRun.period, CheckRun.program_id,
                 CheckRun.broker_party_id)
         .filter(CheckRun.tenant_id == tenant_id,
                 CheckRun.lane.in_(("direct", "canonical")),
                 CheckRun.kind.in_(_REAL_KINDS),
                 CheckRun.chain_key.isnot(None)))
    if program_id:
        q = q.filter(CheckRun.program_id == program_id)
    if broker_party_id:
        q = q.filter(CheckRun.broker_party_id == broker_party_id)
    runs = q.order_by(CheckRun.id.asc()).all()
    if not runs:
        return []
    run_by_id = {r.id: r for r in runs}
    # The checks of each file that really ran over its rows, oldest first —
    # what can show a problem has gone.
    checked: dict[str, list] = defaultdict(list)
    for r in runs:
        if r.status != "not_validated":
            checked[r.chain_key].append(r)

    run_ids = list(run_by_id)
    names: dict[str, str] = {}
    for chunk in _chunks(run_ids):
        for key, name in (s.query(CheckResult.check_key, CheckResult.name)
                          .filter(CheckResult.run_id.in_(chunk))):
            if name:
                names[key] = name

    probs: dict[tuple, dict] = {}
    for chunk in _chunks(run_ids):
        rows = (s.query(CheckError.run_id, CheckError.chain_key, CheckError.fingerprint,
                        CheckError.first_found_at, CheckError.created_at, CheckError.lane,
                        CheckError.export_id, CheckError.sheet, CheckError.row,
                        CheckError.field, CheckError.rule_id, CheckError.policy_number,
                        CheckError.check_key, CheckError.code, CheckError.severity,
                        CheckError.message, CheckError.program_id,
                        CheckError.broker_party_id)
                .filter(CheckError.run_id.in_(chunk)).all())
        for e in rows:
            if not e.chain_key or not e.fingerprint:
                continue
            found = e.first_found_at or e.created_at
            p = probs.get((e.chain_key, e.fingerprint))
            if p is None:
                p = probs[(e.chain_key, e.fingerprint)] = {
                    "chain_key": e.chain_key, "fingerprint": e.fingerprint,
                    "found_at": found, "last_run_id": e.run_id, "occurrences": set()}
            p["found_at"] = min(p["found_at"], found) if found else p["found_at"]
            p["occurrences"].add((e.lane, e.export_id, e.sheet, e.row, e.field,
                                  e.rule_id, (e.policy_number or "").strip()))
            if e.run_id >= p["last_run_id"]:
                # Show the problem as it was last seen.
                run = run_by_id.get(e.run_id)
                p.update(
                    last_run_id=e.run_id,
                    check=names.get(e.check_key) or e.code or e.check_key,
                    check_key=e.check_key, severity=_sev(e.severity),
                    message=e.message, sheet=e.sheet, row=e.row, field=e.field,
                    policy_number=e.policy_number, export_id=e.export_id,
                    program_id=e.program_id, broker_party_id=e.broker_party_id,
                    file=getattr(run, "filename", None), period=getattr(run, "period", None))

    # Decisions on the files these problems were found in, earliest first.
    export_ids = sorted({o[1] for p in probs.values() for o in p["occurrences"] if o[1]})
    direct: dict[tuple, list] = defaultdict(list)
    canonical: dict[tuple, list] = defaultdict(list)
    for chunk in _chunks(export_ids):
        for d in (s.query(ExceptionDecisionLog.export_id, ExceptionDecisionLog.lane,
                          ExceptionDecisionLog.kind, ExceptionDecisionLog.sheet,
                          ExceptionDecisionLog.row, ExceptionDecisionLog.field,
                          ExceptionDecisionLog.rule_id, ExceptionDecisionLog.policy_number,
                          ExceptionDecisionLog.decided_by_user_id,
                          ExceptionDecisionLog.decided_at)
                  .filter(ExceptionDecisionLog.export_id.in_(chunk))
                  .order_by(ExceptionDecisionLog.decided_at.asc()).all()):
            if d.lane == "direct":
                direct[(d.export_id, d.sheet, d.row, d.field)].append(d)
            elif d.lane == "canonical":
                canonical[(d.export_id, d.rule_id, (d.policy_number or "").strip(),
                           d.field)].append(d)

    out = []
    for p in probs.values():
        found = p["found_at"]
        fixed_at, how, by = None, None, None
        for lane, export_id, sheet, row, field, rule_id, policy in p["occurrences"]:
            hits = (direct.get((export_id, sheet, row, field), []) if lane == "direct"
                    else canonical.get((export_id, rule_id, policy, field), []))
            for d in hits:
                if d.decided_at is None or (found and d.decided_at < found):
                    continue
                if fixed_at is None or d.decided_at < fixed_at:
                    fixed_at, how, by = d.decided_at, (d.kind or FIX).lower(), d.decided_by_user_id
                break
        if fixed_at is None:
            later = next((r for r in checked.get(p["chain_key"], [])
                          if r.id > p["last_run_id"]), None)
            if later is not None:
                fixed_at, how = later.created_at, GONE
        out.append({
            "check": p.get("check"), "check_key": p.get("check_key"),
            "severity": p.get("severity"), "message": p.get("message"),
            "sheet": p.get("sheet"), "row": p.get("row"), "field": p.get("field"),
            "policy_number": p.get("policy_number"), "export_id": p.get("export_id"),
            "file": p.get("file"), "period": p.get("period"),
            "program_id": p.get("program_id"), "broker_party_id": p.get("broker_party_id"),
            "found_at": found, "fixed_at": fixed_at,
            "how": how if how in HOW_WORDS else (FIX if fixed_at else None),
            "fixed_by_user_id": by,
        })
    return out


# --- the month read back ----------------------------------------------------------

def _round(x: Optional[float]) -> Optional[float]:
    return round(x, 1) if x is not None else None


def _pct(n: int, d: int) -> Optional[float]:
    return round(100.0 * n / d, 1) if d else None


_SEV_RANK = {"info": 1, "warning": 2, "critical": 3}


def _groups(open_: list[dict]) -> list[dict]:
    """The open problems as one row per file and check — 4,800 rows of the same
    rule on one file read as one line to act on, not 480 pages."""
    out: dict[tuple, dict] = {}
    for p in open_:
        k = (p["export_id"], p["check_key"] or p["check"])
        g = out.get(k)
        if g is None:
            g = out[k] = {"check": p["check"], "severity": p["severity"], "problems": 0,
                          "past_target": 0, "oldest_days": 0.0, "file": p["file"],
                          "period": p["period"], "export_id": p["export_id"],
                          "program_id": p["program_id"],
                          "broker_party_id": p["broker_party_id"]}
        g["problems"] += 1
        g["past_target"] += 1 if p["past_target"] else 0
        g["oldest_days"] = max(g["oldest_days"], p["age_days"])
        if _SEV_RANK.get(p["severity"] or "", 0) > _SEV_RANK.get(g["severity"] or "", 0):
            g["severity"] = p["severity"]
    for g in out.values():
        g["oldest_days"] = _round(g["oldest_days"])
    return sorted(out.values(), key=lambda g: (-(g["oldest_days"] or 0), -g["problems"],
                                               g["check"] or ""))


def summary(s, tenant_id: int, start: datetime, end: datetime, *,
            program_id: Optional[int] = None, broker_party_id: Optional[int] = None,
            now: Optional[datetime] = None, page: int = 1, page_size: int = 10,
            severity: Optional[str] = None) -> dict:
    """41.1 how fast the period's problems were fixed, 41.2 against the
    target, 41.3 what is open NOW — how old, and who has to act.

    `severity` narrows only the open list (`items` and `groups`); the counts
    above it stay the whole picture."""
    now = now or datetime.utcnow()
    target = target_days(s, tenant_id)
    probs = problems(s, tenant_id, program_id=program_id, broker_party_id=broker_party_id)

    fixed = [p for p in probs if p["fixed_at"] and start <= p["fixed_at"] < end]
    took = [_days(p["fixed_at"] - p["found_at"]) for p in fixed if p["found_at"]]
    within = sum(1 for d in took if d <= target)
    open_ = [p for p in probs if not p["fixed_at"]]
    for p in open_:
        p["age_days"] = _days(now - p["found_at"]) if p["found_at"] else 0.0
        p["past_target"] = p["age_days"] > target
    past = [p for p in open_ if p["past_target"]]

    bands = []
    for key, words, lo, hi in AGE_BANDS:
        n = sum(1 for p in open_
                if int(p["age_days"]) >= lo and (hi is None or int(p["age_days"]) <= hi))
        bands.append({"key": key, "label": words, "problems": n,
                      # a band whose youngest member is already past the target
                      "past_target": lo > target})

    by_how = [{"key": k, "label": w, "problems": sum(1 for p in fixed if p["how"] == k)}
              for k, w in HOW_WORDS.items()]

    groups: dict[tuple, dict] = {}

    def _group(p):
        k = (p["broker_party_id"], p["program_id"])
        return groups.setdefault(k, {"broker_party_id": k[0], "program_id": k[1],
                                     "fixed": 0, "_took": [], "within_target": 0,
                                     "open": 0, "past_target": 0, "oldest_days": None})
    for p in fixed:
        g = _group(p)
        g["fixed"] += 1
        if p["found_at"]:
            d = _days(p["fixed_at"] - p["found_at"])
            g["_took"].append(d)
            g["within_target"] += 1 if d <= target else 0
    for p in open_:
        g = _group(p)
        g["open"] += 1
        g["past_target"] += 1 if p["past_target"] else 0
        g["oldest_days"] = max(g["oldest_days"] or 0.0, p["age_days"])
    by_bp = []
    for g in groups.values():
        t = g.pop("_took")
        g["avg_days"] = _round(sum(t) / len(t)) if t else None
        g["within_target_pct"] = _pct(g["within_target"], g["fixed"])
        g["oldest_days"] = _round(g["oldest_days"])
        by_bp.append(g)
    by_bp.sort(key=lambda g: (-g["past_target"], -g["open"], -g["fixed"]))

    listed = [p for p in open_ if not severity or p["severity"] == _sev(severity)]
    open_sorted = sorted(listed, key=lambda p: (-p["age_days"], p["check"] or ""))
    groups = _groups(listed)
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 10), 100))
    shown = open_sorted[(page - 1) * page_size: page * page_size]

    def _iso(v):
        return v.isoformat() if isinstance(v, datetime) else v

    return {
        "from": start.date().isoformat(), "to": (end - timedelta(days=1)).date().isoformat(),
        "target_days": target,
        "fixed": {
            "problems": len(fixed),
            "avg_days": _round(sum(took) / len(took)) if took else None,
            "median_days": _round(median(took)) if took else None,
            "within_target": within,
            "within_target_pct": _pct(within, len(took)),
            "by_how": by_how,
        },
        "found_in_period": sum(1 for p in probs
                               if p["found_at"] and start <= p["found_at"] < end),
        "open": {
            "problems": len(open_),
            "past_target": len(past),
            "oldest_days": _round(max((p["age_days"] for p in open_), default=None)),
            "by_age": bands,
            "page": page, "page_size": page_size, "total": len(open_sorted),
            "items": [{
                "check": p["check"], "severity": p["severity"], "message": p["message"],
                "policy_number": p["policy_number"], "sheet": p["sheet"], "row": p["row"],
                "field": p["field"], "file": p["file"], "period": p["period"],
                "export_id": p["export_id"], "program_id": p["program_id"],
                "broker_party_id": p["broker_party_id"], "found_at": _iso(p["found_at"]),
                "age_days": _round(p["age_days"]), "past_target": p["past_target"],
            } for p in shown],
            # The same list, one row per file and check; paged by the same
            # `page` / `page_size`.
            "groups": groups[(page - 1) * page_size: page * page_size],
            "groups_total": len(groups),
        },
        "by_broker_programme": by_bp,
    }
