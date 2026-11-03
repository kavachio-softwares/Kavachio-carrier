"""Auto-run, for a file whose programme has no live Bordereau Setup yet.

The file is recorded as not run (and kept), the carrier is asked for a setup —
one 'bordereau_setup_needed' row per programme and broker, not per file — and
writing that row can never fail the run. When a setup goes live the waiting
files are queued again (requeue_waiting): the SQL for that is exercised on a
copy of the database, not here.

No database: the session, the recorders and the waiting-files query are
stand-ins.

Run:  python -m pytest test_autorun_no_setup.py
"""
import os

# Never a real database: nothing here needs one, and an import must not find one.
os.environ.setdefault("DATABASE_URL", "sqlite://")

from types import SimpleNamespace

import pytest

import audit
import db
import intake_autorun
import intake_service
import submission_service


class _Session:
    """What run_one reads, by (model, id); anything else is not there."""

    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, model, key):
        return self.rows.get((model.__name__, key))


class _Waiting:
    """Stands in for the waiting-files query: .filter(...).first()."""

    def __init__(self, other):
        self.other = other

    def filter(self, *_a, **_k):
        return self

    def first(self):
        return self.other


@pytest.fixture
def world(monkeypatch):
    """One arrival (41) on programme 7 from broker 12, no live setup; every
    write recorded."""
    w = SimpleNamespace(rows={}, marks=[], events=[], other_waiting=None)
    w.rows[("FileArrival", 41)] = SimpleNamespace(
        id=41, tenant_id=3, program_id=7, matched_broker_party_id=12, route_id=None,
        filename="Atlas_Aug.xlsx", blob_ref="x", resolution=None, contract_id=9)
    w.rows[("Party", 12)] = SimpleNamespace(id=12, legal_name="Atlas Brokers")
    monkeypatch.setattr(db, "SessionLocal", lambda: _Session(w.rows))
    monkeypatch.setattr(intake_service, "mark_run",
                        lambda arrival_id, **kw: w.marks.append((arrival_id, kw)))
    monkeypatch.setattr(audit, "log_activity",
                        lambda *a, **kw: w.events.append((a, kw)))
    monkeypatch.setattr(intake_autorun, "_live_pipeline", lambda *a: None)
    # The broker is still on the programme (the stand-in session cannot query).
    monkeypatch.setattr(intake_service, "broker_on_programme",
                        lambda *a: True, raising=False)
    monkeypatch.setattr(intake_autorun, "_waiting",
                        lambda *a, **kw: _Waiting(w.other_waiting))
    return w


def test_no_live_setup_is_not_run_and_asks_the_carrier_once(world):
    intake_autorun.run_one(41)

    assert world.marks == [(41, {"state": "not_run",
                                 "error": intake_autorun.NO_SETUP_ERROR})]
    assert world.events == [(
        (3, "system", "bordereau_setup_needed"),
        {"target": "program:7",
         "details": {"program_id": 7, "broker_party_id": 12,
                     "broker_name": "Atlas Brokers", "arrival_id": 41,
                     "filename": "Atlas_Aug.xlsx"}})]


def test_a_second_waiting_file_for_the_same_broker_asks_nothing_more(world):
    world.other_waiting = SimpleNamespace(id=40)        # already waiting, already asked
    intake_autorun.run_one(41)

    assert world.marks[0][1]["state"] == "not_run"      # still kept
    assert world.events == []


def test_a_reminder_that_cannot_be_written_never_fails_the_run(world, monkeypatch):
    def broken(*a, **kw):
        raise RuntimeError("activity_events is unreachable")

    monkeypatch.setattr(audit, "log_activity", broken)
    intake_autorun.run_one(41)                          # does not raise
    assert world.marks == [(41, {"state": "not_run",
                                 "error": intake_autorun.NO_SETUP_ERROR})]


def test_the_broker_is_told_the_file_is_on_hold_not_that_it_failed():
    status, note = submission_service._classify_failure(intake_autorun.NO_SETUP_ERROR)
    assert status == "on_hold"
    assert note.startswith("We are looking into a problem on our side")


def test_the_waiting_files_are_found_by_the_sentences_opening_words():
    # Files already waiting carry the older wording; they must still be found.
    assert intake_autorun.NO_SETUP_ERROR.startswith(intake_autorun._NO_SETUP_PREFIX)
    older = ("There is no live setup for this programme yet. Activate one on "
             "the Setup page, then run the file by hand.")
    assert older.startswith(intake_autorun._NO_SETUP_PREFIX)


def test_nothing_to_queue_without_a_tenant_or_programme():
    assert intake_autorun.requeue_waiting(None, 7) == 0
    assert intake_autorun.requeue_waiting(3, None) == 0
