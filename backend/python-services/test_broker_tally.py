"""The Broker Performance card's counting (see broker_tally.py).

No database: the helper is pure, and these are the shapes the card has to get
right — the bar draws `resolved` and `open` as shares of `issues`, and an
`issues` of 0 is what makes the bar go grey.
"""
from broker_tally import broker_latest_tally


def _exc(status=None, note=None, row=1, **kw):
    e = {"sheet": "Sheet1", "row": row, "error_class": "data_violation",
         "rule_id": 7, "status": status}
    if note is not None:
        e["resolution_note"] = note
    e.update(kw)
    return e


def test_resolved_and_open_always_make_up_the_issues():
    excs = [_exc() for _ in range(20)] + [_exc(status="fixed") for _ in range(10)]
    t = broker_latest_tally(excs)
    assert (t["issues"], t["open"], t["resolved"]) == (30, 20, 10)
    assert t["open"] + t["resolved"] == t["issues"]


def test_a_clean_file_reports_no_issues():
    # What draws the grey bar: nothing to split, so the card says "Clean".
    t = broker_latest_tally([])
    assert (t["issues"], t["open"], t["resolved"]) == (0, 0, 0)


def test_decided_statuses_match_the_triage_screen():
    for st in ("approved", "fixed", "dismissed", "rejected"):
        assert broker_latest_tally([_exc(status=st)])["resolved"] == 1
    # A bare "resolved" counts only when its note names the decision.
    assert broker_latest_tally(
        [_exc(status="resolved", note="Fixed by broker")])["resolved"] == 1
    assert broker_latest_tally(
        [_exc(status="resolved", note="looked at it")])["open"] == 1


def test_an_undecided_exception_is_open():
    for st in (None, "", "open", "new"):
        assert broker_latest_tally([_exc(status=st)])["open"] == 1


def test_notices_and_unchecked_rules_are_left_out():
    excs = [_exc(error_class="not_checked"),
            dict(_exc(error_class="not_validated"), rule_id=None),
            _exc()]
    assert broker_latest_tally(excs)["issues"] == 1


def test_an_unchecked_file_is_clean_not_flagged():
    # Every exception filtered away must leave issues at 0, so the row goes
    # grey rather than drawing an empty split.
    assert broker_latest_tally([_exc(error_class="not_checked")])["issues"] == 0


def test_a_not_validated_exception_with_a_rule_still_counts():
    assert broker_latest_tally(
        [_exc(error_class="not_validated", rule_id=4)])["issues"] == 1


def test_junk_in_the_stored_list_is_ignored():
    assert broker_latest_tally([None, "oops", _exc()])["issues"] == 1
    assert broker_latest_tally(None)["issues"] == 0
