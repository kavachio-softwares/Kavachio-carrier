"""One definition of "how much work is on this file", shared by every screen.

Three screens quote the same numbers about the same run — the carrier's Broker
Performance card, the broker's Team Activity card, and the Exception Triage
screen both of them link to — and they used to count them in three places. That
is how a dashboard starts contradicting the screen it opens (see the Broker
Performance card's own history: counting `exception_decision_log` rows reported
"41 fixed" on a file where nothing had changed).

So the counting lives here, once:

  * an exception is counted per EXCEPTION, which is per CELL — a row of forty
    values with one bad date is one thing to fix, not a bad row;
  * a decision is matched to the file's exceptions AS THEY ARE NOW, exactly as
    Exception Triage matches them, so a number here always equals the screen it
    opens — the test itself lives in `broker_tally`, which is tested on its own;
  * notices are left out: `not_checked`, and the bare `not_validated` marker,
    are messages about the run rather than work on a row.
"""
from __future__ import annotations

from sqlalchemy import func


def row_key(e: dict):
    """Which ROW of the bordereau an exception sits on.

    The direct lane identifies a row by (sheet, row) — the same key its
    decisions are stored under; the canonical lane has a policy number. When a
    file gives neither, the exception is its own row rather than being folded
    into a shared "unknown" bucket, which would under-count the work.
    """
    row = e.get("row")
    if row is not None:
        return ("r", e.get("sheet"), str(row))
    pn = str(e.get("policy_number") or "").strip()
    if pn:
        return ("p", pn)
    return ("x", id(e))


def export_tally(r) -> dict:
    """One file's work: rows, exceptions, and how many are still open.

    `rows_flagged` is context for the count ("14 across 10 rows"), never a share
    of the file — one bad cell does not make a bad row.
    """
    from main import _attach_decisions
    # One decision test for every screen: broker_tally's, which has its own
    # tests and is what the carrier's Broker Performance card already used.
    from broker_tally import countable, settled
    excs = countable(_attach_decisions(r.exceptions or [], r))
    flagged: set = set()
    opened = 0
    for e in excs:
        if not settled(e):
            opened += 1
            flagged.add(row_key(e))
    return {
        # A file's row count is what it says it is; the exception rows can only
        # exceed it if a row key is unrecognisable, and then the larger number
        # is the honest one.
        "rows": max(int(r.policy_count or 0), len(flagged)),
        "rows_flagged": len(flagged),
        "exceptions": len(excs),
        "open": opened,
        "put_right": len(excs) - opened,
    }


def current_export_ids(s, *, broker_party_id: int | None = None,
                       tenant_id: int | None = None) -> set[int]:
    """The exports that are still the LIVE result of an upload.

    A "Fix & re-run" writes a NEW export and moves the landing pointer to it,
    but the old export keeps its own exception blob. Counting both would report
    the same problem twice — once as it was, once as it is. So the live set is
    the export a landing still points at (Process Bordereau, the direct lane)
    plus the newest export of each upload (the canonical lane). Superseded runs
    are history, not work in front of anyone.

    Scoped by broker or by carrier, whichever side is asking — the two see the
    same runs from opposite ends.
    """
    from db import LandingRecord, OutputExport

    def _scope(q):
        if broker_party_id is not None:
            q = q.filter(OutputExport.broker_party_id == broker_party_id)
        if tenant_id is not None:
            q = q.filter(OutputExport.tenant_id == tenant_id)
        return q

    live = {i for (i,) in _scope(
        s.query(LandingRecord.output_export_id)
         .join(OutputExport, OutputExport.id == LandingRecord.output_export_id)
         .filter(LandingRecord.output_export_id.isnot(None))).distinct().all()}
    live |= {i for (i,) in _scope(
        s.query(func.max(OutputExport.id))
         .filter(OutputExport.source_upload_id.isnot(None)))
        .group_by(OutputExport.source_upload_id).all()}
    return _newest_versions_only(s, live)


def _newest_versions_only(s, live: set[int]) -> set[int]:
    """Drop the runs a later version of the same submission has replaced.

    A broker who sends September's file again — by any channel — sends the
    next VERSION of one submission, not a second file. Its exceptions are the
    ones that stand, even when there are more of them than before; the older
    version's are history, exactly like a run that was fixed and re-run.
    """
    from db import OutputExport

    seen = {eid: v for eid, v in export_versions(s, live).items()
            if v["submission_ref"] or v["reporting_period"]}
    if not seen:
        return live
    # Newest version within each submission first…
    newest: dict[str, tuple] = {}
    for eid, v in seen.items():
        if v["submission_ref"]:
            key = (v["version_no"] or 0, eid)
            if key > newest.get(v["submission_ref"], (-1, -1)):
                newest[v["submission_ref"]] = key
    standing = {eid for _, eid in newest.values()}
    standing |= {eid for eid, v in seen.items() if not v["submission_ref"]}
    # …then one per broker + programme + contract + period: files from before
    # the period was compulsory can sit in separate submissions for the same
    # month (or in none, only on the calendar), and the latest one stands.
    scope = {eid: (bid, pid, cid) for eid, bid, pid, cid in
             s.query(OutputExport.id, OutputExport.broker_party_id,
                     OutputExport.program_id, OutputExport.contract_id)
             .filter(OutputExport.id.in_(standing)).all()}
    latest: dict[tuple, int] = {}
    for eid in standing:
        v = seen[eid]
        bid, pid, cid = scope.get(eid, (None, None, None))
        group = ((bid, pid, v["contract_id"] or cid, v["reporting_period"])
                 if v["reporting_period"] else ("ref", v["submission_ref"]))
        latest[group] = max(latest.get(group, eid), eid)
    keep = set(latest.values())
    return {eid for eid in live if eid not in seen or eid in keep}


def export_versions(s, export_ids) -> dict[int, dict]:
    """Which submission, version, reporting period and contract each run is.

    Read off the file the run was made from (the arrival's run_export_id, or
    its landing after a Fix & re-run moved the pointer), then the export's own
    stamp (secure-link corrections), then the calendar for the period. A run
    that is part of no submission is simply absent.
    """
    from sqlalchemy import or_
    from db import ExpectedSubmission, LandingRecord, OutputExport, SubmissionVersion
    from intake_models import FileArrival

    ids = {int(i) for i in export_ids if i is not None}
    if not ids:
        return {}
    out: dict[int, dict] = {}

    def _put(eid, ref, ver, period=None, contract_id=None, strong=True):
        d = out.setdefault(eid, {"submission_ref": None, "version_no": None,
                                 "reporting_period": None, "contract_id": None})
        if ref and (strong or not d["submission_ref"]):
            d["submission_ref"], d["version_no"] = ref, ver
        d["reporting_period"] = d["reporting_period"] or period
        d["contract_id"] = d["contract_id"] or contract_id

    arrivals = (s.query(FileArrival.run_export_id, LandingRecord.output_export_id,
                        FileArrival.submission_ref, FileArrival.version_no,
                        FileArrival.reporting_period, FileArrival.contract_id)
                .outerjoin(LandingRecord, LandingRecord.id == FileArrival.run_landing_id)
                .filter(or_(FileArrival.run_export_id.in_(ids),
                            LandingRecord.output_export_id.in_(ids)))
                .order_by(FileArrival.version_no.asc().nullsfirst())
                .all())
    # The file a run was made from first, the export's own stamp second, and a
    # landing's current run last (two files with the same bytes can share one
    # landing), newest version winning.
    for run_eid, _land, ref, ver, per, con in arrivals:
        if run_eid in ids:
            _put(run_eid, ref, ver, per, con)
    for eid, ref, ver, con in (s.query(OutputExport.id, OutputExport.submission_ref,
                                       OutputExport.version_no, OutputExport.contract_id)
                               .filter(OutputExport.id.in_(ids)).all()):
        if ref or eid in out:
            _put(eid, ref, ver, None, con, strong=False)
    stamped = {eid for eid, d in out.items() if d["submission_ref"]}
    for run_eid, land_eid, ref, ver, per, con in arrivals:
        if land_eid in ids and land_eid not in stamped:
            _put(land_eid, ref, ver, per, con)
    missing = [eid for eid, d in out.items() if not d["reporting_period"]]
    missing += [eid for eid in ids if eid not in out]
    if missing:
        for eid, per in (s.query(SubmissionVersion.received_export_id, ExpectedSubmission.period)
                         .join(ExpectedSubmission, ExpectedSubmission.id == SubmissionVersion.expected_id)
                         .filter(SubmissionVersion.received_export_id.in_(missing)).all()):
            if eid in out:
                out[eid]["reporting_period"] = out[eid]["reporting_period"] or per
            else:
                _put(eid, None, None, per)
    return out
