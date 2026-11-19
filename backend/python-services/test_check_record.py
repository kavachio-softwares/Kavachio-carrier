"""MOPR 40 — the shared check record (check_record.py).

Run with:  DATABASE_URL=sqlite:// .venv/bin/python -m pytest test_check_record.py
"""
from __future__ import annotations

from datetime import datetime

import pytest

import check_record as cr


@pytest.fixture(autouse=True, scope="module")
def _jsonb_on_sqlite():
    # file_arrival has a Postgres JSONB column; SQLite stores it as JSON.
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.compiler import compiles

    @compiles(JSONB, "sqlite")
    def _as_json(type_, compiler, **kw):  # noqa: ARG001
        return "JSON"
    yield


def _mem_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import db
    from intake_models import FileArrival
    eng = create_engine("sqlite:///:memory:")
    db.Base.metadata.create_all(
        eng, tables=[db.CheckRun.__table__, db.CheckResult.__table__,
                     db.CheckError.__table__, FileArrival.__table__])
    return sessionmaker(bind=eng)()


STRUCTURE = {"sheets": [{"sheet_name": "Premium", "columns": [
    {"column_name": "Policy Number", "canonical_field": "policy_number"},
    {"column_name": "Txn", "canonical_field": "premium_transaction_type"},
    {"column_name": "Premium"},
]}]}


def _blocks(*rows):
    return [{"sheet": "Premium", "records": [
        {"Policy Number": p, "Txn": t, "Premium": 100} for p, t in rows]}]


# --- reading transaction types and volumes -----------------------------------

@pytest.mark.parametrize("code,kind", [
    ("NB", "new"), ("New Business", "new"), ("R", "renewal"), ("RENEWAL", "renewal"),
    ("CN", "cancellation"), ("FC", "flat_cancellation"),
    ("Endorsement - Extension", "endorsement"),
    ("UNK", "unknown"), ("", "unknown"), (None, "unknown"), ("ZZZ", "other"),
])
def test_transaction_type_is_read_the_way_ingest_reads_it(code, kind):
    assert cr.txn_type(code) == kind


def test_volumes_split_policies_by_transaction_type():
    v = cr.volumes(STRUCTURE, _blocks(("P1", "NB"), ("P2", "NB"), ("P3", "R"),
                                      ("P4", "CN"), ("P1", "EN"), (None, None)))
    # The last row has no policy number: a total, handled but in no type.
    assert v["rows"] == 6
    assert v["policies"] == 4
    t = v["transaction_types"]
    assert t["new"] == {"rows": 2, "policies": 2}
    assert t["renewal"] == {"rows": 1, "policies": 1}
    assert t["cancellation"] == {"rows": 1, "policies": 1}
    assert t["endorsement"] == {"rows": 1, "policies": 1}
    assert "unknown" not in t


def test_volumes_find_columns_by_heading_when_the_template_does_not_say():
    structure = {"sheets": [{"sheet_name": "S", "columns": [
        {"column_name": "Policy No"}, {"column_name": "Transaction Type"}]}]}
    blocks = [{"sheet": "S", "records": [{"Policy No": "A", "Transaction Type": "NB"},
                                         {"Policy No": "B", "Transaction Type": "UNK"}]}]
    v = cr.volumes(structure, blocks)
    assert v["transaction_types"] == {"new": {"rows": 1, "policies": 1},
                                      "unknown": {"rows": 1, "policies": 1}}


def test_volumes_without_a_policy_column_count_rows_only():
    structure = {"sheets": [{"sheet_name": "S", "columns": [{"column_name": "Txn Type"}]}]}
    v = cr.volumes(structure, [{"sheet": "S", "records": [{"Txn Type": "R"}, {"Txn Type": "R"}]}])
    assert v["policies"] is None
    assert v["transaction_types"] == {"renewal": {"rows": 2, "policies": None}}


# --- the checks of one run -----------------------------------------------------

RULES = [{"rule_id": 1, "rule_name": "Premium > 0", "severity": "critical"},
         {"rule_id": 2, "rule_name": "State is valid", "severity": "warning"},
         {"rule_id": 3, "rule_name": "Limit within cover", "severity": "warning"}]


def test_every_rule_is_recorded_passed_failed_or_not_run():
    dv = {"unprocessable": [{"rule_id": 3, "message": "column not filled"}],
          "partially_checked": []}
    errors = [{"rule_id": 1, "row": 1}, {"rule_id": 1, "row": 2},
              {"code": "type_check", "sheet": "Premium", "column": "Inception", "row": 4}]
    res = {r["check_key"]: r for r in cr.check_results(
        RULES, {"Premium": {"Inception": "date", "Expiry": "date"}}, dv, errors)}
    assert res["rule:1"]["outcome"] == cr.FAILED and res["rule:1"]["error_count"] == 2
    assert res["rule:2"]["outcome"] == cr.PASSED
    assert res["rule:3"]["outcome"] == cr.NOT_RUN
    assert res["rule:3"]["message"] == "column not filled"
    assert res["type:Premium:Inception"]["outcome"] == cr.FAILED
    assert res["type:Premium:Expiry"]["outcome"] == cr.PASSED


def test_a_run_that_never_returned_ran_nothing():
    res = cr.check_results(RULES, {"Premium": {"Inception": "date"}}, None, [])
    assert {r["outcome"] for r in res} == {cr.NOT_RUN}


def test_arrival_checks_pass_until_one_stops_the_file():
    order = ["size", "known_sender", "has_rows", "live_contract"]
    res = cr.intake_results(order, "has_rows", "Held — no rows", "held")
    assert [r["outcome"] for r in res] == [cr.PASSED, cr.PASSED, cr.FAILED, cr.NOT_RUN]
    assert res[2]["message"] == "Held — no rows"
    assert all(r["outcome"] == cr.PASSED
               for r in cr.intake_results(order, None, None, "accepted"))


# --- writing a run -------------------------------------------------------------

def _run(s, **kw):
    base = dict(lane=cr.DIRECT, kind=cr.RUN, tenant_id=1, status="has_exceptions",
                rules=RULES, column_types={}, dv={"stats": {"rows_validated": 5}},
                exceptions=[{"rule_id": 1, "row": 1, "sheet": "Premium",
                             "field": "Premium", "policy_number": "P1",
                             "severity": "critical", "message": "Premium must be > 0"},
                            {"error_class": "not_checked", "severity": "info"}],
                structure=STRUCTURE,
                blocks=_blocks(("P1", "NB"), ("P2", "R"), ("P3", "CN")),
                export_id=10, landing_id=20, program_id=7, broker_party_id=8,
                session=s)
    base.update(kw)
    return cr.record_run(**base)


def test_a_run_is_written_with_its_checks_errors_and_volumes():
    import db
    s = _mem_session()
    run_id = _run(s)
    s.commit()
    run = s.get(db.CheckRun, run_id)
    assert (run.checks_total, run.checks_passed, run.checks_failed) == (3, 2, 1)
    # The not-checked notice is about the run, not an error in it.
    assert run.errors_total == 1 and run.critical_count == 1
    assert run.rows_out == 3 and run.rows_checked == 5 and run.rows_with_errors == 1
    assert run.policies == 3
    assert run.transaction_types["cancellation"] == {"rows": 1, "policies": 1}
    assert run.chain_key == "landing:20"
    assert s.query(db.CheckResult).filter_by(run_id=run_id).count() == 3
    err = s.query(db.CheckError).filter_by(run_id=run_id).one()
    assert (err.check_key, err.policy_number) == ("rule:1", "P1")


def test_an_error_keeps_the_time_it_was_first_found_across_re_runs():
    import db
    s = _mem_session()
    first = _run(s)
    s.commit()
    t0 = s.query(db.CheckError).filter_by(run_id=first).one().first_found_at
    again = _run(s, kind=cr.RERUN)
    s.commit()
    e2 = s.query(db.CheckError).filter_by(run_id=again).one()
    assert e2.first_found_at == t0
    assert e2.created_at >= t0


def test_recording_never_costs_the_run():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    s = sessionmaker(bind=create_engine("sqlite:///:memory:"))()   # no tables at all
    assert _run(s) is None


def test_a_corrected_or_released_file_had_a_person_involved():
    import db
    from intake_models import FileArrival
    s = _mem_session()
    s.add(FileArrival(id=1, tenant_id=1, filename="a.xlsx", outcome="accepted",
                      public_ref="R1", submission_ref="SUB-1", version_no=2,
                      reporting_period="2026-09", received_at=datetime(2026, 10, 1)))
    s.add(FileArrival(id=2, tenant_id=1, filename="b.xlsx", outcome="held",
                      resolution="released", public_ref="R2",
                      received_at=datetime(2026, 10, 1)))
    s.flush()
    r1 = s.get(db.CheckRun, _run(s, arrival_id=1, export_id=11))
    r2 = s.get(db.CheckRun, _run(s, arrival_id=2, export_id=12))
    assert r1.human_touch == [cr.CORRECTED]
    assert (r1.chain_key, r1.period, r1.version_no) == ("sub:SUB-1", "2026-09", 2)
    assert r2.human_touch == [cr.RELEASED]


# --- 40.2 / 40.3: the month read back -------------------------------------------

def test_the_month_counts_each_file_once_and_only_files():
    s = _mem_session()
    # File A: clean on arrival — straight through. A re-generate is not a file.
    _run(s, export_id=1, landing_id=1, status="clean", exceptions=[],
         blocks=_blocks(("A1", "NB"), ("A2", "R")))
    _run(s, export_id=1, landing_id=1, kind=cr.RERUN, status="has_exceptions")
    # File B: errors on arrival — not straight through, even after a clean re-run.
    _run(s, export_id=2, landing_id=2, broker_party_id=9,
         blocks=_blocks(("B1", "CN"), ("B2", "UNK")))
    _run(s, export_id=2, landing_id=2, kind=cr.RERUN, status="clean", exceptions=[])
    # Looks at a file, never a file handled.
    _run(s, export_id=3, kind=cr.SELF_CHECK, status="clean", exceptions=[])
    _run(s, export_id=None, lane=cr.CANONICAL, kind=cr.DRY_RUN)
    _run(s, export_id=None, kind=cr.PREVIEW, status="clean", exceptions=[])
    s.commit()

    out = cr.summary(s, 1, *cr.month_bounds(datetime.utcnow().strftime("%Y-%m")))
    v, q, h = out["volumes"], out["quality"], out["no_human_help"]
    assert v["files"] == 2
    assert v["policies"] == 4
    types = {g["key"]: g["policies"] for g in v["transaction_types"]}
    assert types == {"new": 1, "renewed": 1, "cancelled": 1, "other": 0, "unclassified": 1}
    assert q["errors"] == 1 and q["files_clean"] == 1 and q["files_with_errors"] == 1
    assert q["check_runs"] == 7   # every validation run is a check run, files or not
    assert q["top_failing_checks"][0]["check"] == "Premium > 0"
    assert q["top_failing_checks"][0]["severity"] == "critical"
    assert (v["brokers"], v["programmes"]) == (2, 1)
    assert (h["files"], h["files_pct"]) == (1, 50.0)
    by = {b["id"]: b for b in out["by_broker"]}
    assert by[8]["straight_through"] == 1 and by[9]["straight_through"] == 0
    pairs = {(r["broker_party_id"], r["program_id"]): r for r in out["by_broker_programme"]}
    assert set(pairs) == {(8, 7), (9, 7)}
    assert pairs[(8, 7)]["files"] == 1 and pairs[(8, 7)]["straight_through_pct"] == 100.0
    # Most errors first; B's first run had one error on one of its 5 rows checked.
    assert [r["broker_party_id"] for r in out["by_broker_programme"]] == [9, 8]
    assert pairs[(9, 7)]["rows_with_errors"] == 1 and pairs[(9, 7)]["rows_with_errors_pct"] == 20.0


def test_a_clean_correction_is_not_straight_through():
    from intake_models import FileArrival
    s = _mem_session()
    s.add(FileArrival(id=5, tenant_id=1, filename="c.xlsx", outcome="accepted",
                      public_ref="R5", version_no=2, received_at=datetime(2026, 10, 1)))
    s.flush()
    _run(s, export_id=1, arrival_id=5, status="clean", exceptions=[])
    s.commit()
    out = cr.summary(s, 1, *cr.month_bounds(datetime.utcnow().strftime("%Y-%m")))
    assert out["volumes"]["corrected_files"] == 1
    assert out["no_human_help"]["files"] == 0


def test_a_month_outside_the_record_is_empty():
    s = _mem_session()
    _run(s)
    s.commit()
    out = cr.summary(s, 1, *cr.month_bounds("2020-01"))
    assert out["volumes"]["files"] == 0
    assert out["no_human_help"]["files_pct"] is None
