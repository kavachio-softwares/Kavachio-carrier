"""A replaced contract's files: kept and listed, not counted as work.

Each contract keeps its own file history (Version 1, 2… per contract). When a
contract is replaced and the NEW contract has a file for the same month, the
old contract's file for that month is history kept for audit: it stays on the
lists (labelled) and in the calendar's Versions panel, but leaves every
current-work count. A month only the old contract has a file for stays counted.

A RENEWED contract is different: its files and exceptions stay live work —
counted and editable — whatever the renewal has sent.
"""
from datetime import datetime

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(_type, _compiler, **_kw):  # file_arrival.itd_summary
    return "JSON"


def _mem_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import db
    from intake_models import FileArrival
    eng = create_engine("sqlite:///:memory:")
    db.Base.metadata.create_all(
        eng, tables=[db.Program.__table__, db.Contract.__table__,
                     db.OutputExport.__table__, db.LandingRecord.__table__,
                     db.ExpectedSubmission.__table__, db.SubmissionVersion.__table__,
                     FileArrival.__table__])
    return sessionmaker(bind=eng)()


BROKER = 7


def _contract(s, prog, *, replaced=False, renews=None):
    import db
    c = db.Contract(program_id=prog.id, tenant_id=1, broker_party_id=BROKER,
                    renews_contract_id=renews.id if renews is not None else None,
                    name=f"Contract {'old' if replaced else 'new'}",
                    lifecycle="superseded" if replaced else "active",
                    status="superseded" if replaced else "active")
    s.add(c); s.flush()
    return c


def _file(s, prog, contract, period, *, ref, version):
    """One processed file: its output, and the arrival that says which
    submission, version, month and contract it is."""
    import db
    from intake_models import FileArrival
    out = db.OutputExport(tenant_id=1, filename=f"{ref}-v{version}.xlsx",
                          program_id=prog.id, broker_party_id=BROKER,
                          contract_id=contract.id, source_upload_id=None,
                          status="has_exceptions", exception_count=3)
    s.add(out); s.flush()
    out.source_upload_id = 10_000 + out.id  # its own upload, so it is live
    # Its own id: SQLite does not number a BIGINT key by itself.
    s.add(FileArrival(id=out.id, tenant_id=1, filename=out.filename, outcome="accepted",
                      received_at=datetime(2026, 10, 1), program_id=prog.id,
                      matched_broker_party_id=BROKER, run_export_id=out.id,
                      reporting_period=period, submission_ref=ref,
                      version_no=version, contract_id=contract.id))
    s.flush()
    return out


@pytest.fixture
def s():
    import db
    sess = _mem_session()
    prog = db.Program(tenant_id=1, name="prg test"); sess.add(prog); sess.flush()
    sess.prog = prog
    yield sess
    sess.close()


def test_new_contracts_file_answers_the_month(s):
    from exception_tally import contract_flags, current_export_ids
    old, new = _contract(s, s.prog, replaced=True), _contract(s, s.prog)
    old_aug = _file(s, s.prog, old, "2026-08", ref="inb_old", version=15)
    new_aug = _file(s, s.prog, new, "2026-08", ref="inb_new", version=4)

    assert current_export_ids(s, broker_party_id=BROKER) == {new_aug.id}
    # Kept for audit: listed when asked for, labelled, not counted.
    assert current_export_ids(s, broker_party_id=BROKER, include_replaced=True) \
        == {old_aug.id, new_aug.id}
    flags = contract_flags(s, [old_aug.id, new_aug.id])
    assert flags[old_aug.id] == {"contract_replaced": True, "contract_renewed": False,
                                 "counted": False}
    assert flags[new_aug.id] == {"contract_replaced": False, "contract_renewed": False,
                                 "counted": True}


def test_month_only_the_old_contract_has_stays_counted(s):
    from exception_tally import contract_flags, current_export_ids
    old, new = _contract(s, s.prog, replaced=True), _contract(s, s.prog)
    _file(s, s.prog, new, "2026-08", ref="inb_new", version=1)
    old_sep = _file(s, s.prog, old, "2026-09", ref="inb_old_sep", version=2)

    assert old_sep.id in current_export_ids(s, broker_party_id=BROKER)
    assert contract_flags(s, [old_sep.id])[old_sep.id] == {
        "contract_replaced": True, "contract_renewed": False, "counted": True}


def test_two_contracts_in_force_are_both_work(s):
    from exception_tally import current_export_ids
    a, b = _contract(s, s.prog), _contract(s, s.prog)
    fa = _file(s, s.prog, a, "2026-08", ref="inb_a", version=1)
    fb = _file(s, s.prog, b, "2026-08", ref="inb_b", version=1)
    assert current_export_ids(s, broker_party_id=BROKER) == {fa.id, fb.id}


def test_chain_of_replacements_keeps_only_the_latest_link(s):
    from exception_tally import current_export_ids
    first = _contract(s, s.prog, replaced=True)
    second = _contract(s, s.prog, replaced=True)
    _file(s, s.prog, first, "2026-08", ref="inb_1", version=3)
    newer = _file(s, s.prog, second, "2026-08", ref="inb_2", version=1)
    assert current_export_ids(s, broker_party_id=BROKER) == {newer.id}


def test_each_contract_keeps_its_own_version_numbers(s):
    """The calendar names a file by its number within its contract, the way
    Exception Triage does — not the period's running count."""
    from submission_calendar_service import _file_contracts
    old, new = _contract(s, s.prog, replaced=True), _contract(s, s.prog)
    o = _file(s, s.prog, old, "2026-08", ref="inb_old", version=15)
    n = _file(s, s.prog, new, "2026-08", ref="inb_new", version=4)
    facts = _file_contracts(s, {o.id, n.id})
    assert facts[o.id]["version_no"] == 15 and facts[o.id]["contract_replaced"] is True
    assert facts[n.id]["version_no"] == 4 and facts[n.id]["contract_replaced"] is False


def test_renewed_contracts_exceptions_stay_counted(s):
    """Your rule (19 Oct): a renewed contract's exceptions are preserved, can
    still be worked, and stay in every dashboard count."""
    from exception_tally import contract_flags, current_export_ids
    old = _contract(s, s.prog, replaced=True)
    renewal = _contract(s, s.prog, renews=old)
    old_aug = _file(s, s.prog, old, "2026-08", ref="inb_old", version=3)
    new_aug = _file(s, s.prog, renewal, "2026-08", ref="inb_new", version=1)

    assert current_export_ids(s, broker_party_id=BROKER) == {old_aug.id, new_aug.id}
    assert contract_flags(s, [old_aug.id])[old_aug.id] == {
        "contract_replaced": False, "contract_renewed": True, "counted": True}
