"""
Renewal history: every contract period linked by renewal, oldest first, seen
the same from any contract in the chain. No database: contracts are stood in
for and the access check is patched.
"""
import datetime as dt
import types

import pytest
from fastapi import HTTPException

import contract_routes as cr


def _k(id, renews=None, lifecycle="active", expiry=None, broker=9, name=None):
    return types.SimpleNamespace(
        id=id, renews_contract_id=renews, lifecycle=lifecycle, status=lifecycle,
        inception_dt=dt.date(2020 + id % 10, 10, 1), expiry_dt=expiry,
        name=name, filename=f"c{id}.pdf", tenant_id=3, broker_party_id=broker)


def _walk(rows, start):
    by = {r.id: r for r in rows}
    succ = lambda cid: min((r for r in rows if r.renews_contract_id == cid),
                           key=lambda r: r.id, default=None)
    return [k.id for k in cr._renewal_chain(by[start], by.get, succ)]


def test_single_contract_is_its_own_chain():
    assert _walk([_k(1)], 1) == [1]


@pytest.mark.parametrize("start", [1, 2, 3])
def test_three_versions_same_from_start_middle_end(start):
    rows = [_k(1), _k(2, renews=1), _k(3, renews=2)]
    assert _walk(rows, start) == [1, 2, 3]


def test_two_versions():
    assert _walk([_k(10), _k(11, renews=10)], 10) == [10, 11]


def test_broken_link_stops_cleanly():
    # 5 renews 4, but 4 is gone
    assert _walk([_k(5, renews=4), _k(6, renews=5)], 6) == [5, 6]


def test_loop_does_not_spin():
    rows = [_k(1, renews=2), _k(2, renews=1)]
    chain = _walk(rows, 1)
    assert sorted(chain) == [1, 2] and len(chain) == 2


class _Q:
    def __init__(self, rows): self.rows, self.cid = rows, None
    def filter(self, cond):
        self.cid = cond.right.value
        return self
    def order_by(self, *_): return self
    def first(self):
        m = [r for r in self.rows if r.renews_contract_id == self.cid]
        return min(m, key=lambda r: r.id) if m else None


class _S:
    def __init__(self, rows): self.rows = rows
    def get(self, _m, cid): return next((r for r in self.rows if r.id == cid), None)
    def query(self, _m): return _Q(self.rows)


def test_history_entries_and_hidden_version(monkeypatch):
    rows = [_k(1, lifecycle="superseded"), _k(2, renews=1, broker=8),
            _k(3, renews=2, lifecycle="draft")]
    s = _S(rows)

    def access(_s, _p, cid):
        if s.get(None, cid).broker_party_id != 9:
            raise HTTPException(404, "contract not found")
    monkeypatch.setattr(cr, "_contract_access", access)
    hist = cr._renewal_history(s, rows[2], p=object())
    assert [(h["id"], h["version"]) for h in hist] == [(1, 1), (3, 3)]
    assert [h["lifecycle"] for h in hist] == ["superseded", "draft"]
    assert [h["is_current"] for h in hist] == [False, True]


def test_never_renewed_gives_empty_history():
    assert cr._renewal_history(_S([_k(1)]), _k(1), p=None) == []
