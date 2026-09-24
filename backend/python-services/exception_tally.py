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
    return live
