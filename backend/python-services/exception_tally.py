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


def correction_bases(s, export_ids) -> dict[int, int]:
    """{export id: the export of the file as the broker FIRST sent it} for each
    run that is a later version of its submission.

    Every new version is checked again from scratch, so the exceptions the
    broker resolved stay on the versions before it: a Fix & Validate or
    secure-link correction reads "0 exceptions, 0 resolved", and so does a
    corrected file sent again by upload, email, SFTP or API (20 Nov 2026 — the
    usual way brokers clean a file; Harborline Oct went 75 → 20 exceptions by
    re-upload and the dashboard said 0 resolved). The work done is measured
    from the first version that was run — Partner Performance's "problems
    fixed while cleaning" rule, so the two screens agree. A run that is the
    first version, or in no submission, is absent."""
    from submission_service import load

    vers = export_versions(s, export_ids)
    out: dict[int, int] = {}
    threads: dict = {}
    for eid, v in vers.items():
        ref = v.get("submission_ref")
        if not ref:
            continue
        if ref not in threads:
            try:
                threads[ref] = load(s, ref)
            except Exception:  # noqa: BLE001 — a list never fails on one submission
                threads[ref] = None
        th = threads[ref]
        if th is None:
            continue
        chain = [x for x in th.versions if x.export_id]
        if not chain or not any(x.export_id == eid for x in chain):
            continue
        base = chain[0].export_id
        if base != eid:
            out[eid] = base
    return out


def with_earlier_fixes(now: dict, base: dict | None) -> dict:
    """One file's tally (export_tally of its newest version) with the
    exceptions resolved on the versions before it counted in.

    exceptions  what the file had as the broker sent it (`base`), or what the
                newest version has, if that is more;
    open        still open on the newest version;
    put_right   exceptions − open — "9 of 9 resolved", the rule Partner
                Performance uses for "problems fixed while cleaning".
    Rows stay the newest version's."""
    if not base:
        return now
    found = max(base["exceptions"], now["exceptions"])
    return {**now, "exceptions": found, "put_right": max(0, found - now["open"])}


def submission_tallies(s, exports) -> dict[int, dict]:
    """{export id: export_tally with earlier fixes counted} for these export
    ROWS — what a list of live files shows. One base export is read at a time
    and let go: the exceptions column runs to megabytes."""
    exports = [r for r in exports if r is not None]
    bases = correction_bases(s, [r.id for r in exports])
    cache: dict = {}
    return {r.id: tally_with_fixes(s, r, bases, cache) for r in exports}


def tally_with_fixes(s, r, bases: dict[int, int], cache: dict) -> dict:
    """One row of submission_tallies, for loops that read their exports one
    at a time: `bases` from correction_bases, `cache` any dict kept across
    the loop (a base export's tally, read once)."""
    from db import OutputExport

    b = bases.get(r.id)
    if b and b not in cache:
        row = s.get(OutputExport, b)
        cache[b] = export_tally(row) if row is not None else None
        if row is not None:
            s.expunge(row)
    return with_earlier_fixes(export_tally(r), cache.get(b) if b else None)


def current_export_ids(s, *, broker_party_id: int | None = None,
                       tenant_id: int | None = None,
                       include_replaced: bool = False) -> set[int]:
    """The exports that are still the LIVE result of an upload.

    A "Fix & re-run" writes a NEW export and moves the landing pointer to it,
    but the old export keeps its own exception blob. Counting both would report
    the same problem twice — once as it was, once as it is. So the live set is
    the export a landing still points at (Process Bordereau, the direct lane)
    plus the newest export of each upload (the canonical lane). Superseded runs
    are history, not work in front of anyone.

    Scoped by broker or by carrier, whichever side is asking — the two see the
    same runs from opposite ends.

    A file under a contract that has since been REPLACED is left out once the
    newer contract has a file for the same month (`replaced_contract_runs`):
    it is kept for audit, not counted as work. Lists that show it with a
    "Replaced contract" label pass `include_replaced=True`.
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
    live = _newest_versions_only(s, live)
    if not include_replaced:
        live -= replaced_contract_runs(s, live)
    return live


def is_replaced_contract(lifecycle, status) -> bool:
    """A contract a newer one has taken over — a renewal gone in force, or a
    new contract uploaded for the same programme and broker. Read from either
    state column: the two are written together (contract_routes,
    direct_routes) but older rows carry only one."""
    return "superseded" in ((lifecycle or "").strip(), (status or "").strip())


def superseded_kinds(s, cids) -> dict[int, str]:
    """{contract id: "renewed" | "replaced"} for the superseded ones.

    RENEWED — a later contract renews it (contract_renews_contract_id): the
    old term's files and exceptions stay live work, counted and editable.
    REPLACED — taken over any other way (a new contract uploaded for the same
    programme and broker): its files are kept for audit and leave the counts
    once the new contract has a file for the same month."""
    from db import Contract

    cids = {c for c in cids if c}
    if not cids:
        return {}
    gone = {cid for cid, life, st in
            s.query(Contract.id, Contract.lifecycle, Contract.status)
            .filter(Contract.id.in_(cids)).all() if is_replaced_contract(life, st)}
    if not gone:
        return {}
    renewed = {r for (r,) in s.query(Contract.renews_contract_id)
               .filter(Contract.renews_contract_id.in_(gone)).all()}
    return {cid: ("renewed" if cid in renewed else "replaced") for cid in gone}


def replaced_contract_runs(s, live: set[int]) -> set[int]:
    """Of these runs, the ones a NEWER contract's file has answered.

    Each contract keeps its own history (Version 1, 2… per contract), so one
    month can hold the newest file under the old contract AND the newest under
    the contract that replaced it. Both stay in the database and on the lists;
    only the newer one is work. The old file is history kept for audit.

    Only when the newer contract has a file for the SAME broker, programme and
    month, and never for a RENEWED contract: a renewal's predecessor keeps its
    exceptions as live work, counted and editable (superseded_kinds). A month
    nobody has re-sent under the new contract keeps its only file counted —
    nothing is dropped for want of a replacement.
    """
    from db import Contract, OutputExport

    if not live:
        return set()
    vers = export_versions(s, live)
    scope = {eid: (bid, pid, cid) for eid, bid, pid, cid in
             s.query(OutputExport.id, OutputExport.broker_party_id,
                     OutputExport.program_id, OutputExport.contract_id)
             .filter(OutputExport.id.in_(live)).all()}
    groups: dict[tuple, list[tuple[int, int]]] = {}
    for eid in live:
        v = vers.get(eid) or {}
        bid, pid, cid = scope.get(eid, (None, None, None))
        cid = v.get("contract_id") or cid
        if not v.get("reporting_period") or cid is None or pid is None:
            continue
        groups.setdefault((bid, pid, v["reporting_period"]), []).append((eid, cid))
    mixed = [g for g in groups.values() if len({c for _, c in g}) > 1]
    if not mixed:
        return set()
    cids = {c for g in mixed for _, c in g}
    replaced = {cid for cid, kind in superseded_kinds(s, cids).items()
                if kind == "replaced"}
    out: set[int] = set()
    for g in mixed:
        # The file that answers the month: under a contract still in force if
        # there is one, else the newest — a chain of replacements keeps only
        # its latest link counted.
        rank = lambda item: (item[1] not in replaced, item[0])  # noqa: E731
        best = max(g, key=rank)
        out |= {eid for eid, cid in g if cid in replaced and cid != best[1]}
    return out


def contract_flags(s, export_ids) -> dict[int, dict]:
    """{export id: {"contract_replaced", "contract_renewed", "counted"}}.

    `contract_replaced` / `contract_renewed` — the contract the file was
    checked against has been replaced, or renewed (labels, whatever the
    month). `counted` — False when a newer contract's file has answered the
    same month of a REPLACED contract, so the file is history kept for audit
    and not part of the open work (`replaced_contract_runs`). A renewed
    contract's files are always counted.
    """
    from db import OutputExport

    ids = {int(i) for i in export_ids if i is not None}
    if not ids:
        return {}
    vers = export_versions(s, ids)
    own = {eid: cid for eid, cid in s.query(OutputExport.id, OutputExport.contract_id)
           .filter(OutputExport.id.in_(ids)).all()}
    cid_of = {eid: (vers.get(eid) or {}).get("contract_id") or own.get(eid) for eid in ids}
    kinds = superseded_kinds(s, cid_of.values())
    replaced = {c for c, k in kinds.items() if k == "replaced"}
    if not replaced:
        return {eid: {"contract_replaced": False,
                      "contract_renewed": kinds.get(cid_of.get(eid)) == "renewed",
                      "counted": True} for eid in ids}
    # Whether a newer contract answered the month is a question about the
    # broker's whole live set, not just this page of the list.
    bids = {b for (b,) in s.query(OutputExport.broker_party_id)
            .filter(OutputExport.id.in_(ids)).distinct().all()}
    answered: set[int] = set()
    for b in bids:
        answered |= replaced_contract_runs(
            s, current_export_ids(s, broker_party_id=b, include_replaced=True)
            if b is not None else ids)
    return {eid: {"contract_replaced": cid_of.get(eid) in replaced,
                  "contract_renewed": kinds.get(cid_of.get(eid)) == "renewed",
                  "counted": eid not in answered} for eid in ids}


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


# The decision kinds that settle an exception (broker_tally counts all four).
SETTLING = ("fix", "approve", "dismiss", "reject")


def resolved_by_day(s, *, tenant_id: int | None = None,
                    broker_party_id: int | None = None,
                    program_id: int | None = None,
                    start=None, end=None) -> dict:
    """{date: exceptions resolved that day} for the "Resolved Exceptions In
    Files" line — counted the way the Broker Performance card counts them.

    The line used to count decision rows only, so the exceptions a broker
    resolved by sending a corrected version (upload, email, SFTP, API, Fix &
    Validate) never showed: Harborline Oct went 75 → 16 by re-sending, the
    card said 59 resolved and the chart 3 (20 Nov 2026).

    Each submission (or a file in none) has, at the end of any day, a
    "resolved so far" — with_earlier_fixes' rule on the version that stood
    then: exceptions found − still open, where still open adds back the
    decisions on that version made later. A day's figure is how much that
    rose (or fell) that day, summed over the submissions, so the days add up
    to the card's number. A day where a worse version came in can come out
    below 0; callers draw it as 0. `start` / `end` (dates, inclusive) keep only
    the days inside; None leaves that side open."""
    import datetime as _dt
    from db import ExceptionDecisionLog, OutputExport
    from submission_service import load

    def _scope(q):
        if tenant_id is not None:
            q = q.filter(OutputExport.tenant_id == tenant_id)
        if broker_party_id is not None:
            q = q.filter(OutputExport.broker_party_id == broker_party_id)
        if program_id is not None:
            q = q.filter(OutputExport.program_id == program_id)
        return q

    def _inside(col):
        out = []
        if start is not None:
            out.append(func.date(col) >= start)
        if end is not None:
            out.append(func.date(col) <= end)
        return out

    # The files with something happening in the window: a version made, or a
    # decision taken on one.
    active = {i for (i,) in _scope(s.query(OutputExport.id))
              .filter(*_inside(OutputExport.created_at)).all()}
    active |= {i for (i,) in _scope(s.query(OutputExport.id)
               .join(ExceptionDecisionLog, ExceptionDecisionLog.export_id == OutputExport.id))
               .filter(ExceptionDecisionLog.kind.in_(SETTLING),
                       *_inside(ExceptionDecisionLog.decided_at)).all()}
    if not active:
        return {}

    # Each one's whole submission, oldest version first.
    chains: dict = {}
    vers = export_versions(s, active)
    threads: dict = {}
    for eid in active:
        ref = (vers.get(eid) or {}).get("submission_ref")
        chain = None
        if ref:
            if ref not in threads:
                try:
                    th = load(s, ref)
                except Exception:  # noqa: BLE001 — a chart never fails on one submission
                    th = None
                threads[ref] = [x.export_id for x in th.versions if x.export_id] if th else []
            if eid in threads[ref]:
                chain = tuple(threads[ref])
        chains[chain or (eid,)] = True

    ids = {e for c in chains for e in c}
    made = {i: c for i, c in s.query(OutputExport.id, OutputExport.created_at)
            .filter(OutputExport.id.in_(ids)).all()}
    later: dict = {}                 # {export: [(date, n)]} settling decisions
    for eid, d, n in (s.query(ExceptionDecisionLog.export_id,
                              func.date(ExceptionDecisionLog.decided_at),
                              func.count(ExceptionDecisionLog.id))
                      .filter(ExceptionDecisionLog.export_id.in_(ids),
                              ExceptionDecisionLog.kind.in_(SETTLING))
                      .group_by(ExceptionDecisionLog.export_id,
                                func.date(ExceptionDecisionLog.decided_at)).all()):
        later.setdefault(eid, []).append((_dt.date.fromisoformat(str(d)), n))
    tallies: dict = {}
    for eid in ids:
        r = s.get(OutputExport, eid)
        if r is None:
            continue
        tallies[eid] = export_tally(r)
        s.expunge(r)                 # the exceptions column runs to megabytes

    out: dict = {}
    for chain in chains:
        chain = [e for e in chain if e in tallies and made.get(e) is not None]
        if not chain:
            continue
        base = tallies[chain[0]]

        def resolved_at(day):
            standing = [e for e in chain if made[e].date() <= day]
            if not standing:
                return 0
            v = standing[-1]
            t = tallies[v]
            undone = sum(n for d, n in later.get(v, ()) if d > day)
            still_open = min(t["exceptions"], t["open"] + undone)
            return max(0, max(base["exceptions"], t["exceptions"]) - still_open)

        days = sorted({made[e].date() for e in chain}
                      | {d for e in chain for d, _ in later.get(e, ())})
        before = 0
        for day in days:
            now = resolved_at(day)
            if now != before and (start is None or day >= start) and (end is None or day <= end):
                out[day] = out.get(day, 0) + now - before
            before = now
    return out
