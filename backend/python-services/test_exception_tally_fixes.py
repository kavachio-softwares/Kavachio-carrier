"""Exceptions resolved before a later version still count as resolved.

Fix & Validate sends the broker's corrections in as the NEXT version, which is
checked from scratch: it reads "0 exceptions, 0 resolved" on its own, and the
carrier's lists showed the broker's work as nothing. exception_tally now walks
back over correction versions to the file as the broker sent it.

No database: the submission thread and the version lookup are stand-ins.
"""
import sys
import types

import pytest

import exception_tally as et


class V:
    def __init__(self, no, export_id, source):
        self.no, self.export_id, self.source = no, export_id, source


class Thread:
    def __init__(self, versions):
        self.versions = versions


@pytest.fixture
def thread(monkeypatch):
    """One submission: v1 uploaded (export 10), v2 Fix & Validate (11),
    v3 held, never run (no export), v4 secure link (13); and a second
    submission whose newest version is a fresh upload (export 21)."""
    threads = {
        "SUB-A": Thread([V(1, 10, "upload"), V(2, 11, "fix_validate"),
                         V(3, None, "email"), V(4, 13, "secure_link")]),
        "SUB-B": Thread([V(1, 20, "upload"), V(2, 21, "email")]),
    }
    fake = types.ModuleType("submission_service")
    fake.CORRECTION_SOURCES = ("secure_link", "fix_validate")
    fake.load = lambda s, ref: threads.get(ref)
    monkeypatch.setitem(sys.modules, "submission_service", fake)
    refs = {10: "SUB-A", 11: "SUB-A", 13: "SUB-A", 20: "SUB-B", 21: "SUB-B", 99: None}
    monkeypatch.setattr(et, "export_versions", lambda s, ids: {
        i: {"submission_ref": refs.get(i), "version_no": None,
            "reporting_period": "2026-10", "contract_id": None} for i in ids})
    return threads


def test_a_correction_points_back_to_the_file_as_sent(thread):
    assert et.correction_bases(None, [11, 13]) == {11: 10, 13: 10}


def test_a_corrected_file_sent_again_points_back_to_the_first_version(thread):
    # 20 Nov 2026: a re-upload / email resend is how most brokers clean a file.
    assert et.correction_bases(None, [21]) == {21: 20}


def test_a_first_version_and_a_file_in_no_submission_have_no_base(thread):
    assert et.correction_bases(None, [10, 20, 99]) == {}


def test_all_resolved_by_fix_and_validate():
    now = {"rows": 50, "rows_flagged": 0, "exceptions": 0, "open": 0, "put_right": 0}
    base = {"rows": 50, "rows_flagged": 9, "exceptions": 9, "open": 9, "put_right": 0}
    t = et.with_earlier_fixes(now, base)
    assert (t["exceptions"], t["open"], t["put_right"]) == (9, 0, 9)
    assert t["rows"] == 50 and t["rows_flagged"] == 0     # rows stay the newest version's


def test_some_still_open_on_the_new_version():
    now = {"rows": 5, "rows_flagged": 2, "exceptions": 2, "open": 2, "put_right": 0}
    base = {"rows": 5, "rows_flagged": 5, "exceptions": 9, "open": 9, "put_right": 0}
    assert et.with_earlier_fixes(now, base)["put_right"] == 7


def test_more_on_the_new_version_than_before_never_goes_negative():
    now = {"rows": 5, "rows_flagged": 4, "exceptions": 12, "open": 12, "put_right": 0}
    base = {"rows": 5, "rows_flagged": 3, "exceptions": 3, "open": 3, "put_right": 0}
    t = et.with_earlier_fixes(now, base)
    assert (t["exceptions"], t["open"], t["put_right"]) == (12, 12, 0)


def test_no_base_leaves_the_tally_as_it_was():
    now = {"rows": 5, "rows_flagged": 1, "exceptions": 4, "open": 1, "put_right": 3}
    assert et.with_earlier_fixes(now, None) is now


def test_tally_with_fixes_reads_each_base_once(thread, monkeypatch):
    reads = []

    class Row:
        def __init__(self, i):
            self.id = i

    class Session:
        def get(self, model, i):
            reads.append(i)
            return Row(i)

        def expunge(self, r):
            pass

    tallies = {10: {"rows": 1, "rows_flagged": 1, "exceptions": 9, "open": 9, "put_right": 0},
               11: {"rows": 1, "rows_flagged": 0, "exceptions": 0, "open": 0, "put_right": 0},
               13: {"rows": 1, "rows_flagged": 0, "exceptions": 0, "open": 0, "put_right": 0}}
    monkeypatch.setattr(et, "export_tally", lambda r: tallies[r.id])
    fake_db = types.ModuleType("db")
    fake_db.OutputExport = object
    monkeypatch.setitem(sys.modules, "db", fake_db)
    out = et.submission_tallies(Session(), [Row(11), Row(13)])
    assert out[11]["put_right"] == 9 and out[13]["put_right"] == 9
    assert reads == [10]
