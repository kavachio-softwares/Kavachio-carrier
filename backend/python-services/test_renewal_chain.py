"""renewal_chain: which term a file belongs to, pins that follow the chain,
and re-dating copied rules. No database."""
import datetime as dt
import types

import pytest

import renewal_chain as rc

D = dt.date


def _c(id, start, end, renews=None, name=None):
    return types.SimpleNamespace(id=id, inception_dt=start, expiry_dt=end,
                                 renews_contract_id=renews, name=name or f"C{id}")


OLD = _c(1, D(2025, 10, 1), D(2026, 10, 1))
NEW = _c(2, D(2026, 10, 1), D(2027, 10, 1), renews=1)


def test_chain_of_links_renewal_to_old_and_leaves_others():
    other = _c(9, D(2025, 1, 1), D(2026, 1, 1))
    key = rc.chain_of([OLD, NEW, other])
    assert key[1] == key[2] and key[9] != key[1]


@pytest.mark.parametrize("label,want", [
    ("2026-10", 2),      # first month of the renewal (old expiry is exclusive)
    ("2027-09", 2),
    ("2026-09", 1),      # late file for the old term
    ("2025-10", 1),
])
def test_route_by_period_picks_the_covering_term(label, want):
    cid, why = rc.route_by_period([OLD, NEW], 1, label)
    assert (cid, why) == (want, None)


@pytest.mark.parametrize("label", ["2024-05", "2028-01", "Q3 2026", None, ""])
def test_route_by_period_holds_when_unclear(label):
    cid, why = rc.route_by_period([OLD, NEW], 1, label)
    assert cid is None and why.startswith("Held")


def test_overlapping_terms_are_held():
    overlap = _c(3, D(2026, 6, 1), D(2027, 6, 1), renews=1)
    cid, why = rc.route_by_period([OLD, overlap], 1, "2026-07")
    assert cid is None and "more than one term" in why


def test_follow_pins_swaps_only_same_chain():
    pins = {"Written": 1, "Claims": 7}
    assert rc.follow_pins(pins, 2, {1, 2}) == {"Written": 2, "Claims": 7}
    assert rc.follow_pins(pins, 1, {1, 2}) == pins          # late file: old rules
    assert rc.follow_pins(pins, 5, {5}) == pins             # unrelated contract


def test_period_start():
    assert rc.period_start("2026-10") == D(2026, 10, 1)
    assert rc.period_start("2026-10-15") == D(2026, 10, 15)
    assert rc.period_start("2026-Q4") is None


MOVES = rc.term_moves(D(2025, 10, 1), D(2026, 10, 1), D(2026, 10, 1), D(2027, 10, 1))


def test_redate_moves_each_date_once_when_new_start_is_old_end():
    out = rc.redate_rule(
        {"ir": {"params": {"date": "2025-10-01"}},
         "compiled_sql": "a >= '2025-10-01' AND b < '2026-10-01'"},
        "Inception on or after 2025-10-01",
        "Must be on or after October 1, 2025.", MOVES)
    assert out["spec"]["ir"]["params"]["date"] == "2026-10-01"
    assert out["spec"]["compiled_sql"] == "a >= '2026-10-01' AND b < '2027-10-01'"
    assert out["name"] == "Inception on or after 2026-10-01"
    assert out["message"] == "Must be on or after October 1, 2026."
    assert out["changed"] and out["other_dates"] == []


def test_redate_leaves_unrelated_dates_and_flags_them():
    out = rc.redate_rule({"compiled_sql": "d = '2024-05-05'"}, "x", "after 21 October 2025", MOVES)
    assert out["spec"]["compiled_sql"] == "d = '2024-05-05'"
    assert out["message"] == "after 21 October 2025"     # not "1 October 2025" inside it
    assert out["other_dates"] == ["2024-05-05"] and not out["changed"]


def test_redate_ignores_ambiguous_numeric_forms():
    out = rc.redate_rule(None, None, "Due 01/10/2025", MOVES)
    assert out["message"] == "Due 01/10/2025"


def test_no_moves_when_dates_unknown():
    assert rc.term_moves(None, None, D(2026, 1, 1), D(2027, 1, 1)) == {}


def test_switch_off(monkeypatch):
    monkeypatch.setenv("RENEWAL_CHAIN_ROUTING", "false")
    assert not rc.routing_enabled()
