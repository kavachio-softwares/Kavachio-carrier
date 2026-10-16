"""
B5 — "Make it active" on an uploaded renewal. contract_routes._renewal_activation
is the list the page shows and _check_renewal_activation refuses on, so the two
cannot disagree. Nothing is waived: signed copy, read, references, people,
dates. A gap is asked about once (D2). No database.
"""
import datetime as dt
import types

import pytest
from fastapi import HTTPException

import contract_routes as cr

D = dt.date
CARRIER = types.SimpleNamespace(is_broker=False, user_id=9, tenant_id=6)
BROKER = types.SimpleNamespace(is_broker=True, user_id=50, tenant_id=6)


def renewal(**kw):
    base = dict(id=20, tenant_id=6, renews_contract_id=10, lifecycle="draft",
                inception_dt=D(2026, 10, 1), expiry_dt=D(2027, 9, 30))
    base.update(kw)
    return types.SimpleNamespace(**base)


def doc(kind="contract", executed=True, active=True):
    return types.SimpleNamespace(kind=kind, is_executed_copy=executed or None,
                                 is_active=active)


OVERLAP = {"code": "overlaps_previous", "severity": "block", "days": 5,
           "with_id": 10, "message": "both would claim 5 days"}
GAP = {"code": "gap_after_previous", "severity": "warn", "days": 8,
       "with_id": 10, "message": "8 days belong to no contract"}


@pytest.fixture
def world(monkeypatch):
    """A renewal that is ready unless a test says otherwise."""
    w = types.SimpleNamespace(uploaded=True, clauses=True, parties=None,
                              conflicts=[], confirmed=False)
    monkeypatch.setattr(cr, "_effective_lifecycle", lambda c: c.lifecycle)
    monkeypatch.setattr(cr, "_is_uploaded", lambda c: w.uploaded)
    monkeypatch.setattr(cr, "_has_clauses", lambda s, cid: w.clauses)

    def parties(s, p, c):
        if w.parties:
            raise HTTPException(403, w.parties)
    monkeypatch.setattr(cr, "_recheck_renewal_parties", parties)
    monkeypatch.setattr(cr, "_period_conflicts", lambda s, c, succ=None: list(w.conflicts))
    monkeypatch.setattr(cr, "_gap_already_confirmed", lambda s, c, g: w.confirmed)
    monkeypatch.delenv("RENEWAL_OVERLAP_CHECK", raising=False)
    return w


def act(c=None, docs=(), missing=(), due=False, p=CARRIER):
    return cr._renewal_activation(None, c or renewal(), list(docs), list(missing), due, p)


def codes(a):
    return [b["code"] for b in a["blockers"]]


# ── when it applies ────────────────────────────────────────────────────────

def test_not_shown_for_a_contract_that_renews_nothing(world):
    assert act(renewal(renews_contract_id=None)) is None


def test_not_shown_to_a_broker(world):
    assert act(p=BROKER) is None


def test_not_shown_once_active(world):
    assert act(renewal(lifecycle="active")) is None


def test_not_shown_on_the_authored_road(world):
    world.uploaded = False
    assert act(due=False) is None


# ── what is in the way ─────────────────────────────────────────────────────

def test_ready_when_nothing_is_missing(world):
    a = act(docs=[doc()])
    assert a == {"ready": True, "blockers": [], "gaps": []}


def test_same_wording_renewal_has_no_document_and_is_ready(world):
    # "No — keep the previous contract": clauses copied, no new PDF.
    assert act(docs=[])["ready"] is True


def test_signed_copy_not_attached(world):
    world.uploaded = False
    a = act(due=True)
    assert a["ready"] is False and codes(a) == ["signed_copy"]


def test_pdf_not_marked_as_the_signed_copy(world):
    a = act(docs=[doc(executed=False)])
    assert codes(a) == ["signed_copy_unmarked"]


def test_a_retired_signed_copy_does_not_count(world):
    a = act(docs=[doc(executed=True, active=False), doc(executed=False)])
    assert codes(a) == ["signed_copy_unmarked"]


def test_unread_pdf(world):
    world.clauses = False
    assert codes(act(docs=[doc()])) == ["unread"]


def test_missing_references(world):
    a = act(docs=[doc()], missing=["Underwriting Guidelines"])
    assert codes(a) == ["references"]
    assert "Underwriting Guidelines" in a["blockers"][0]["message"]


def test_broker_no_longer_on_the_programme(world):
    world.parties = "this broker is no longer on the programme"
    a = act(docs=[doc()])
    assert codes(a) == ["parties"]
    assert a["blockers"][0]["message"] == "this broker is no longer on the programme"


def test_broker_recorded_under_another_organisation_is_named(world, monkeypatch):
    def parties(s, p, c):
        raise HTTPException(404, "contract not found")
    monkeypatch.setattr(cr, "_recheck_renewal_parties", parties)
    import db
    monkeypatch.setattr(db, "carrier_broker_ids", lambda s, tid: set())   # not linked
    other = types.SimpleNamespace(tenant_id=1, legal_name="Broker Org")
    s = types.SimpleNamespace(get=lambda model, pid: other)
    a = cr._renewal_activation(s, renewal(broker_party_id=771), [doc()], [], False, CARRIER)
    msg = a["blockers"][0]["message"]
    assert "Broker Org" in msg and "not found" not in msg


def test_overlapping_dates(world):
    world.conflicts = [OVERLAP]
    a = act(docs=[doc()])
    assert codes(a) == ["dates"] and a["blockers"][0]["with_id"] == 10


def test_every_reason_is_listed_together(world):
    world.clauses = False
    world.conflicts = [OVERLAP]
    a = act(docs=[doc(executed=False)], missing=["Guidelines"])
    assert codes(a) == ["signed_copy_unmarked", "unread", "references", "dates"]


def test_overlap_switch_off_skips_only_the_dates(world, monkeypatch):
    monkeypatch.setenv("RENEWAL_OVERLAP_CHECK", "false")
    world.conflicts = [OVERLAP, GAP]
    world.clauses = False
    a = act(docs=[doc()])
    assert codes(a) == ["unread"] and a["gaps"] == []


# ── gaps (D2) ──────────────────────────────────────────────────────────────

def test_unconfirmed_gap_is_asked_not_blocking(world):
    world.conflicts = [GAP]
    a = act(docs=[doc()])
    assert a["ready"] is True and a["gaps"] == [GAP]


def test_gap_confirmed_earlier_is_not_asked_again(world):
    world.conflicts = [GAP]
    world.confirmed = True
    assert act(docs=[doc()])["gaps"] == []


# ── the server refuses on the same list ────────────────────────────────────

class _Q:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return self.rows


class _S:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def query(self, *a, **k):
        return _Q(self.rows)


def check(world, monkeypatch, confirmed=False, docs=(doc(),), due=False):
    monkeypatch.setattr(cr, "_missing_references", lambda c, d: [])
    monkeypatch.setattr(cr, "_awaiting_signed_copy", lambda s, c: due)
    return cr._check_renewal_activation(_S(docs), renewal(), CARRIER,
                                        confirmed=confirmed)


def test_refusal_names_the_first_reason_and_carries_errors(world, monkeypatch):
    world.clauses = False
    with pytest.raises(HTTPException) as e:
        check(world, monkeypatch)
    d = e.value.detail
    assert e.value.status_code == 409
    assert d["message"].startswith("This renewal cannot go in force yet: the new contract")
    assert "errors" in d and [b["code"] for b in d["blockers"]] == ["unread"]


def test_signed_copy_due_is_named_not_called_written_here(world, monkeypatch):
    world.uploaded = False
    with pytest.raises(HTTPException) as e:
        check(world, monkeypatch, docs=(), due=True)
    assert "signed renewal contract" in e.value.detail["message"]


def test_unconfirmed_gap_needs_confirm(world, monkeypatch):
    world.conflicts = [GAP]
    with pytest.raises(HTTPException) as e:
        check(world, monkeypatch)
    assert e.value.detail["needs_confirm"] is True and "errors" in e.value.detail


def test_confirmed_gap_goes_ahead_and_is_returned_for_the_log(world, monkeypatch):
    world.conflicts = [GAP]
    assert check(world, monkeypatch, confirmed=True) == [GAP]


def test_ready_without_gap_goes_ahead(world, monkeypatch):
    assert check(world, monkeypatch) == []


def test_not_an_uploaded_renewal_is_not_checked_here(world, monkeypatch):
    world.uploaded = False
    assert check(world, monkeypatch, docs=(), due=False) == []


# ── which audit rows count as "already confirmed" ──────────────────────────

def confirmed(rows, gap=GAP, c=None):
    return cr._gap_already_confirmed(_S(rows), c or renewal(), gap)


def test_confirmed_on_the_renewal_itself():
    rows = [("contract:20", {"gaps": [{"code": "gap_after_previous", "with_id": 10, "days": 8}]})]
    assert confirmed(rows) is True


def test_confirmed_from_the_previous_versions_side():
    rows = [("contract:10", {"gaps": [{"code": "gap_before_next", "with_id": 20, "days": 8}]})]
    assert confirmed(rows) is True


def test_a_different_length_is_a_different_gap():
    rows = [("contract:20", {"gaps": [{"code": "gap_after_previous", "with_id": 10, "days": 6}]})]
    assert confirmed(rows) is False


def test_a_gap_with_another_contract_does_not_count():
    rows = [("contract:20", {"gaps": [{"code": "gap_before_next", "with_id": 99, "days": 8}]})]
    assert confirmed(rows) is False


def test_no_rows():
    assert confirmed([]) is False


# ── a broker shared between carriers (7758, 16 Oct 2026) ───────────────────

def test_carrier_works_with_its_own_a_shared_or_a_linked_broker(monkeypatch):
    import db
    monkeypatch.setattr(db, "carrier_broker_ids", lambda s, tid: {771} if tid == 588 else set())
    assert cr._carrier_works_with(None, 5, 588, 588)          # its own party
    assert cr._carrier_works_with(None, 5, None, 588)         # a tenant-less party
    assert cr._carrier_works_with(None, 771, 1, 588)          # recorded under 1, linked to 588
    assert not cr._carrier_works_with(None, 772, 1, 588)      # recorded under 1, not linked
    assert not cr._carrier_works_with(None, 771, 1, 600)      # linked to 588 only


def _counterparty_world(monkeypatch, linked):
    import db
    monkeypatch.setattr(db, "carrier_broker_ids", lambda s, tid: linked)
    party = types.SimpleNamespace(tenant_id=1, party_type="broker", legal_name="Broker Org")
    link = types.SimpleNamespace(status="active")

    class Q:
        def filter(self, *a):
            return self
        def first(self):
            return link
    monkeypatch.setattr("carrier_scope.link_is_live", lambda link, p: True)
    return types.SimpleNamespace(get=lambda model, pid: party, query=lambda *a: Q())


def test_counterparty_linked_shared_broker_is_accepted(monkeypatch):
    s = _counterparty_world(monkeypatch, {771})
    spec = {"counterparty_party_type": "broker", "label": "Insurer ↔ Broker",
            "counterparty_must_be_on_programme": True}
    cr._check_counterparty(s, CARRIER, spec, 651, 771, 588)   # no exception


def test_counterparty_unlinked_foreign_broker_stays_not_found(monkeypatch):
    s = _counterparty_world(monkeypatch, set())
    spec = {"counterparty_party_type": "broker", "label": "Insurer ↔ Broker",
            "counterparty_must_be_on_programme": True}
    with pytest.raises(HTTPException) as e:
        cr._check_counterparty(s, CARRIER, spec, 651, 771, 588)
    assert e.value.status_code == 404
