"""Phase 2 · 44 — open_problems.build: filters, counts, owners and paging.

No database: build() is pure, and every source hands it the same item shape.
"""
from datetime import date, timedelta

import open_problems as op

TODAY = date(2026, 11, 4)


def _p(kind, *, started, due, owner, key, program_id=1, broker=None):
    return op._item(kind, started=started, due=due, status="s", owner=owner,
                    program_id=program_id, broker_party_id=broker, title="t", key=key)


def _sample():
    return [
        _p(op.CHECK, started=TODAY - timedelta(days=9), due=TODAY - timedelta(days=4),
           owner=7, broker=7, key="a"),
        _p(op.CHECK, started=TODAY - timedelta(days=1), due=TODAY + timedelta(days=4),
           owner=7, broker=7, key="b"),
        _p(op.LATE_FILE, started=TODAY - timedelta(days=3), due=TODAY - timedelta(days=3),
           owner=8, broker=8, key="c"),
        _p(op.DISPUTE, started=TODAY - timedelta(days=2), due=TODAY + timedelta(days=3),
           owner=op.CARRIER, broker=8, key="d"),
        _p(op.HELD_FILE, started=TODAY, due=TODAY + timedelta(days=5),
           owner=op.CARRIER, key="e"),
    ]


def test_counts_by_type_and_overdue():
    out = op.build(_sample(), today=TODAY)
    by = {t["type"]: t for t in out["by_type"]}
    assert out["total_open"] == 5 and out["total_overdue"] == 2
    assert by[op.CHECK] == {"type": op.CHECK, "label": "Failed check", "open": 2, "overdue": 1}
    assert by[op.LATE_FILE]["overdue"] == 1 and by[op.SIGNATURE]["open"] == 0


def test_due_today_is_not_overdue():
    out = op.build([_p(op.CHECK, started=TODAY - timedelta(days=5), due=TODAY,
                       owner=7, key="x")], today=TODAY)
    assert out["items"][0]["overdue"] is False and out["items"][0]["age_days"] == 5


def test_workload_is_per_company_most_overdue_first():
    out = op.build(_sample(), today=TODAY)
    owners = [w["owner"] for w in out["workload"]]
    assert owners[0] in ("7", "8")             # one overdue each, broker 7 has more open
    assert owners[0] == "7"
    carrier = next(w for w in out["workload"] if w["owner"] == op.CARRIER)
    assert carrier["open"] == 2 and carrier["overdue"] == 0
    assert carrier["by_type"][op.DISPUTE] == 1 and carrier["by_type"][op.HELD_FILE] == 1
    assert next(w for w in out["workload"] if w["owner"] == "7")["oldest_days"] == 9


def test_filters_narrow_the_list_not_the_counts():
    out = op.build(_sample(), today=TODAY, owner=op.CARRIER)
    assert [i["key"] for i in out["items"]] == ["d", "e"]
    assert out["total"] == 2 and out["total_open"] == 5
    assert op.build(_sample(), today=TODAY, kind=op.CHECK)["total"] == 2
    assert {i["key"] for i in op.build(_sample(), today=TODAY, overdue_only=True)["items"]} == {"a", "c"}


def test_sorts_and_paging():
    by_age = op.build(_sample(), today=TODAY)
    assert [i["key"] for i in by_age["items"]] == ["a", "c", "d", "b", "e"]
    by_due = op.build(_sample(), today=TODAY, sort="due")
    assert [i["key"] for i in by_due["items"]][:2] == ["a", "c"]
    page2 = op.build(_sample(), today=TODAY, page=2, page_size=2)
    assert [i["key"] for i in page2["items"]] == ["d", "b"] and page2["total"] == 5


def test_missing_dates_do_not_break():
    out = op.build([_p(op.SIGNATURE, started=None, due=None, owner=op.CARRIER, key="z")],
                   today=TODAY, sort="due")
    item = out["items"][0]
    assert item["age_days"] is None and item["overdue"] is False and item["due_on"] is None
