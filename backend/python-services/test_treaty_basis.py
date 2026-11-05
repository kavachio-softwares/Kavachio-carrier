"""
27.1 / 27.2 — a treaty's calculation basis, with the dates it applies from,
tested scenario by scenario: each worked through to the statement it produces.

Every scenario reads its own policy from the period's statement (all the test
treaties protect the same programme, so a period's statement carries every
scenario's rows).
"""
import os

import pytest

import treaty_basis as tb
import treaty_routes
from auth_tokens import mint_access_token
from db import AppUser, Contract, ContractDocument, SessionLocal, TreatyTerm
from test_ceded import _approve_basis, _approve_endorsements, _inward, _qs, _treaty, client, mail, w  # noqa: F401 — fixtures

pytestmark = pytest.mark.usefixtures("mail")


# ── helpers ─────────────────────────────────────────────────────────────────
def _change(w, cid, term, value, on, layer=None, approve=True, **kw):
    """A dated change — and, unless `approve` is False, sent to the reinsurers
    and approved by every one, so it is used."""
    r = client.post(f"/contracts/{cid}/basis", headers=w["ch"], json={
        "term": term, "layer": layer, "value": value, "effective_from": on, **kw})
    if approve and r.status_code == 200:
        _approve_basis(w, cid)
    return r


def _statement(w, cid, period):
    r = client.post("/reinsurance/bordereaux", headers=w["ch"],
                    json={"contract_id": cid, "period": period})
    assert r.status_code == 200, r.text
    return r.json()


def _lines(cb, policy):
    return [ln for ln in cb["lines"] if ln.get("policy") == policy]


def _versions(w, cid, term, layer=None):
    b = client.get(f"/contracts/{cid}/basis", headers=w["ch"]).json()
    return next(t for t in b["terms"] if t["term"] == term and t["layer"] == layer)["versions"]


def _active(cid):
    with SessionLocal() as s:
        s.get(Contract, cid).lifecycle = "active"
        s.commit()


def _xol(w, retention=1_000_000, limit=1_000_000):
    return {"treaty_type": "xol",
            "terms": {"currency": "USD", "cover_basis": "per_risk", "basis": "losses_occurring"},
            "layers": [{"name": "Layer 1", "terms": {"retention": retention, "limit": limit},
                        "shares": [{"reinsurer_party_id": w["swiss"], "share_pct": 100,
                                    "is_lead": True}]}]}


def _surplus(w, line=100_000, lines=4):
    return {"treaty_type": "surplus",
            "terms": {"currency": "USD", "retention_line": line, "lines": lines,
                      "ceding_commission_pct": 25},
            "layers": [{"name": "Surplus", "terms": {},
                        "shares": [{"reinsurer_party_id": w["swiss"], "share_pct": 100,
                                    "is_lead": True}]}]}


# ── 27.1: the dated calculation basis ───────────────────────────────────────
def test_s1_a_term_changed_twice_applies_each_value_in_its_own_window(w):
    """40% from inception, 30% from 1 April, 20% from 1 September."""
    for per in ("2027-02", "2027-05", "2027-10"):
        _inward(w, [("S1", 100_000, 1000, None, None)], period=per)
    cid = _treaty(w, _qs(w, 40), "S1 two changes")
    assert _change(w, cid, "cession_pct", 30, "2027-04-01").status_code == 200
    assert _change(w, cid, "cession_pct", 20, "2027-09-01").status_code == 200
    assert [(v["effective_from"], v["value"]) for v in _versions(w, cid, "cession_pct")] == \
        [("2027-01-01", 40), ("2027-04-01", 30), ("2027-09-01", 20)]
    got = {per: _lines(_statement(w, cid, per), "S1")[0] for per in ("2027-02", "2027-05", "2027-10")}
    assert {p: (ln["ceded_pct"], ln["ceded_premium"]) for p, ln in got.items()} == {
        "2027-02": (40, 400), "2027-05": (30, 300), "2027-10": (20, 200)}


def test_s2_a_change_on_the_inception_date_replaces_the_agreed_value(w):
    _inward(w, [("S2", 100_000, 1000, None, None)], period="2027-01")
    cid = _treaty(w, _qs(w, 40), "S2 on inception")
    assert _change(w, cid, "cession_pct", 25, "2027-01-01").status_code == 200
    assert _lines(_statement(w, cid, "2027-01"), "S2")[0]["ceded_pct"] == 25


def test_s3_a_mid_month_change_starts_with_the_next_period(w):
    """A statement uses the terms in force on its period's FIRST day: a change
    from 15 May leaves May at the old rate, June at the new."""
    for per in ("2027-05", "2027-06"):
        _inward(w, [("S3", 100_000, 1000, None, None)], period=per)
    cid = _treaty(w, _qs(w, 40), "S3 mid-month")
    assert _change(w, cid, "cession_pct", 30, "2027-05-15").status_code == 200
    assert _lines(_statement(w, cid, "2027-05"), "S3")[0]["ceded_pct"] == 40
    assert _lines(_statement(w, cid, "2027-06"), "S3")[0]["ceded_pct"] == 30


def test_s4_a_quarterly_statement_uses_the_terms_on_the_quarters_first_day(w):
    for per in ("2027-08", "2027-11"):
        _inward(w, [("S4", 100_000, 1000, None, None)], period=per)
    cid = _treaty(w, _qs(w, 40), "S4 quarterly", frequency="quarterly")
    assert _change(w, cid, "cession_pct", 30, "2027-08-01").status_code == 200
    assert _lines(_statement(w, cid, "2027-Q3"), "S4")[0]["ceded_pct"] == 40
    assert _lines(_statement(w, cid, "2027-Q4"), "S4")[0]["ceded_pct"] == 30


def test_s5_a_layer_limit_raised_mid_year_raises_the_recovery(w):
    """XOL 1m xs 1m; a 3m claim recovers 1m — 1.5m once the limit is raised."""
    for per in ("2027-02", "2027-11"):
        _inward(w, [("S5", None, None, f"CL5-{per}", 3_000_000)], period=per)
    cid = _treaty(w, _xol(w), "S5 XOL limit")
    assert _change(w, cid, "limit", 1_500_000, "2027-07-01").status_code == 400  # which layer?
    assert _change(w, cid, "limit", 1_500_000, "2027-07-01", layer=3).status_code == 400
    assert _change(w, cid, "limit", 1_500_000, "2027-07-01", layer=0).status_code == 200
    assert [v["value"] for v in _versions(w, cid, "limit", 0)] == [1_000_000, 1_500_000]
    rec = lambda per: _lines(_statement(w, cid, per), "S5")[0]["recovery"]   # noqa: E731
    assert (rec("2027-02"), rec("2027-11")) == (1_000_000, 1_500_000)


def test_s6_a_layer_attachment_changed_moves_where_recovery_starts(w):
    _inward(w, [("S6", None, None, "CL6", 3_000_000)], period="2027-12")
    cid = _treaty(w, _xol(w, retention=1_000_000, limit=5_000_000), "S6 XOL attachment")
    assert _change(w, cid, "retention", 2_500_000, "2027-06-01", layer=0).status_code == 200
    assert _lines(_statement(w, cid, "2027-12"), "S6")[0]["recovery"] == 500_000


def test_s7_a_surplus_with_fewer_lines_cedes_less(w):
    """Retention 100k, 4 lines: a 500k risk cedes 80%; with 2 lines, 40%."""
    for per in ("2027-01", "2027-04"):
        _inward(w, [("S7", 500_000, 1000, None, None)], period=per)
    cid = _treaty(w, _surplus(w), "S7 surplus lines")
    assert _change(w, cid, "lines", 2.5, "2027-03-01").status_code == 400     # whole lines
    assert _change(w, cid, "lines", 2, "2027-03-01").status_code == 200
    pct = lambda per: _lines(_statement(w, cid, per), "S7")[0]["ceded_pct"]   # noqa: E731
    assert (pct("2027-01"), pct("2027-04")) == (80, 40)


# ── 27.2: commission terms ─────────────────────────────────────────────────
def test_s8_a_ceding_commission_change_moves_commission_and_net(w):
    _inward(w, [("S8", 100_000, 1000, None, None)], period="2027-03")
    cid = _treaty(w, _qs(w, 40), "S8 commission")                 # 30% commission
    assert _change(w, cid, "ceding_commission_pct", 20, "2027-02-01").status_code == 200
    ln = _lines(_statement(w, cid, "2027-03"), "S8")[0]
    assert (ln["ceded_premium"], ln["ceding_commission"], ln["net_premium"]) == (400, 80, 320)


def test_s9_a_sliding_scale_charges_its_provisional_rate_until_removed(w):
    _inward(w, [("S9", 100_000, 1000, None, None)], period="2027-04")
    cid = _treaty(w, _qs(w, 40), "S9 sliding")                    # 30% commission
    scale = {"provisional_pct": 22, "method": "linear",
             "points": [{"loss_ratio": 70, "rate": 15}, {"loss_ratio": 40, "rate": 30}]}
    r = _change(w, cid, "sliding_scale", scale, "2027-01-01", approve=False)
    assert r.status_code == 200, r.text
    saved = _versions(w, cid, "sliding_scale")[-1]["value"]
    assert [p["loss_ratio"] for p in saved["points"]] == [40, 70]             # sorted
    # Not approved yet: the agreed 30% is still what is charged.
    assert _lines(_statement(w, cid, "2027-04"), "S9")[0]["ceding_commission"] == 120
    _approve_basis(w, cid)
    assert _lines(_statement(w, cid, "2027-04"), "S9")[0]["ceding_commission"] == 88   # 22% of 400
    # Approved by every reinsurer: it stays.
    assert client.delete(f"/contracts/{cid}/basis/{_versions(w, cid, 'sliding_scale')[-1]['id']}",
                         headers=w["ch"]).status_code == 409


@pytest.mark.parametrize("lr, linear, step", [
    (0, 35, 35), (50, 35, 35), (55, 32.5, 35), (60, 30, 30), (65, 27.5, 30),
    (70, 25, 25), (95, 25, 25)])
def test_s10_the_sliding_scale_rate_at_each_loss_ratio(lr, linear, step):
    scale = tb.clean_value("sliding", {"provisional_pct": 30, "points": [
        {"loss_ratio": 50, "rate": 35}, {"loss_ratio": 60, "rate": 30},
        {"loss_ratio": 70, "rate": 25}]})
    assert tb.sliding_rate(scale, lr) == linear
    assert tb.sliding_rate({**scale, "method": "step"}, lr) == step


def test_s11_profit_commission_years():
    pc = tb.clean_value("profit", {"rate_pct": 20, "expense_pct": 5, "deficit_years": 2})
    good = tb.profit_commission(pc, premium=200_000, ceding_commission=60_000, losses=80_000)
    assert (good["expenses"], good["profit"], good["commission"]) == (10_000, 50_000, 10_000)
    bad = tb.profit_commission(pc, premium=200_000, ceding_commission=60_000, losses=170_000)
    assert bad["commission"] == 0 and bad["deficit_carried_forward"] == 40_000
    # Next year's profit is reduced by the deficit brought forward.
    nxt = tb.profit_commission(pc, premium=200_000, ceding_commission=60_000, losses=80_000,
                               deficit_brought_forward=40_000)
    assert (nxt["profit"], nxt["commission"]) == (10_000, 2_000)
    # No carry-forward agreed: a deficit is not carried.
    pc0 = tb.clean_value("profit", {"rate_pct": 20})
    assert tb.profit_commission(pc0, premium=100_000, ceding_commission=30_000,
                                losses=90_000)["deficit_carried_forward"] == 0
    # A sliding scale on no premium sits at the top of the scale — nothing to adjust.
    scale = tb.clean_value("sliding", {"provisional_pct": 30, "points": [
        {"loss_ratio": 50, "rate": 35}, {"loss_ratio": 70, "rate": 25}]})
    assert tb.sliding_adjustment(scale, premium=0, losses=0)["adjustment"] == 0


# ── what is refused ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("term, value, on, says", [
    ("cession_pct", 101, "2027-06-01", "at most 100"),
    ("cession_pct", -5, "2027-06-01", "negative"),
    ("cession_pct", "abc", "2027-06-01", "number"),
    ("cession_pct", 30, "2026-12-31", "incepts"),
    ("cession_pct", 30, "2028-01-01", "expires"),
    ("cession_pct", 30, "", "date"),
    ("rate_on_line_pct", 5, "2027-06-01", "no such term"),
    ("lines", 3, "2027-06-01", "no such term"),               # a quota share has no lines
    ("profit_commission", {"rate_pct": 0}, "2027-06-01", "rate"),
    ("profit_commission", {"rate_pct": 20, "expense_pct": 120}, "2027-06-01", "expense"),
    ("sliding_scale", {"provisional_pct": 30, "points": [{"loss_ratio": 50, "rate": 35}]},
     "2027-06-01", "two points"),
    ("sliding_scale", {"provisional_pct": 30, "points": [
        {"loss_ratio": 50, "rate": 35}, {"loss_ratio": 50, "rate": 25}]}, "2027-06-01", "once"),
])
def test_s12_a_change_that_does_not_make_sense_is_refused(w, term, value, on, says):
    cid = _treaty(w, _qs(w, 40), f"S12 {term} {on}", setup=False)
    r = _change(w, cid, term, value, on)
    assert r.status_code == 400, r.text
    assert says in r.text.lower(), r.text


def test_s13_two_changes_on_one_date_and_removing(w):
    cid = _treaty(w, _qs(w, 40), "S13 duplicate", setup=False)
    assert _change(w, cid, "cession_pct", 30, "2027-06-01", approve=False).status_code == 200
    r = _change(w, cid, "cession_pct", 25, "2027-06-01", approve=False)
    assert r.status_code == 400 and "already a change" in r.text
    entry = _versions(w, cid, "cession_pct")[-1]["id"]
    assert client.delete(f"/contracts/{cid}/basis/{entry}", headers=w["ch"]).status_code == 200
    assert client.delete(f"/contracts/{cid}/basis/{entry}", headers=w["ch"]).status_code == 404
    with SessionLocal() as s:                                  # kept, marked removed
        assert s.get(TreatyTerm, entry).removed_at is not None
    # Once removed, the date is free again.
    assert _change(w, cid, "cession_pct", 25, "2027-06-01", approve=False).status_code == 200
    # Another treaty's change cannot be removed through this one.
    other = _treaty(w, _qs(w, 40), "S13 other", setup=False)
    mine = _versions(w, cid, "cession_pct")[-1]["id"]
    assert client.delete(f"/contracts/{other}/basis/{mine}", headers=w["ch"]).status_code == 404


def test_s14_a_carrier_user_reads_the_basis_but_only_an_admin_changes_it(w):
    cid = _treaty(w, _qs(w, 40), "S14 roles", setup=False)
    with SessionLocal() as s:
        f = s.query(AppUser).filter(AppUser.role == "kavachio_admin").order_by(AppUser.id).first()
        u = AppUser(tenant_id=w["t"], email=f"u14-{os.urandom(3).hex()}@x.test",
                    full_name="Una", role="carrier_admin", invited_by_user_id=f.id)
        s.add(u); s.commit()
        # Not the tenant's owner: a carrier USER, who reads but does not commit.
        uh = {"Authorization": f"Bearer {mint_access_token(u.id, w['t'], 'carrier_admin')}"}
    b = client.get(f"/contracts/{cid}/basis", headers=uh)
    assert b.status_code == 200 and b.json()["can_edit"] is False
    assert client.post(f"/contracts/{cid}/basis", headers=uh, json={
        "term": "cession_pct", "value": 30, "effective_from": "2027-06-01"}).status_code == 403


# ── statements follow the basis — drafts, not what was sent ─────────────────
def test_s15_a_draft_follows_a_change_without_preparing_it_again(w):
    _inward(w, [("S15", 100_000, 1000, None, None)], period="2027-08")
    cid = _treaty(w, _qs(w, 40), "S15 draft follows")
    first = _statement(w, cid, "2027-08")
    assert _lines(first, "S15")[0]["ceded_pct"] == 40
    assert _change(w, cid, "cession_pct", 35, "2027-01-01").status_code == 200
    client.get("/reinsurance/bordereaux", headers=w["ch"])      # the automatic pass
    again = client.get(f"/reinsurance/bordereaux/{first['id']}", headers=w["ch"]).json()
    assert again["id"] == first["id"] and again["status"] == "draft"
    assert _lines(again, "S15")[0]["ceded_pct"] == 35


def test_s16_a_sent_statement_is_not_rewritten_but_says_its_terms_changed(w):
    _inward(w, [("S16", 100_000, 1000, None, None)], period="2027-12")
    cid = _treaty(w, _qs(w, 40), "S16 sent")
    cb = _statement(w, cid, "2027-12")
    assert client.post(f"/reinsurance/bordereaux/{cb['id']}/send", headers=w["ch"],
                       json={}).status_code == 200
    assert client.get(f"/reinsurance/bordereaux/{cb['id']}",
                      headers=w["ch"]).json()["basis_changed"] is False
    assert _change(w, cid, "cession_pct", 35, "2027-01-01").status_code == 200
    client.get("/reinsurance/bordereaux", headers=w["ch"])
    sent = client.get(f"/reinsurance/bordereaux/{cb['id']}", headers=w["ch"]).json()
    assert sent["status"] != "draft" and _lines(sent, "S16")[0]["ceded_pct"] == 40
    assert sent["basis_changed"] is True
    assert len(sent["versions"]) == 1                          # no correction issued by itself


# ── endorsements move the basis ─────────────────────────────────────────────
def _endorse(w, cid, on, basis, limits=None):
    return client.post(f"/contracts/{cid}/endorsement", headers=w["ch"], json={
        "agreed_limits": limits or {}, "effective_from": on, "basis": basis})


def _endorsements(cid):
    with SessionLocal() as s:
        return s.query(ContractDocument).filter(ContractDocument.contract_id == cid,
                                                ContractDocument.kind == "endorsement").count()


def test_s17_an_endorsement_raises_a_layer_limit_from_its_date(w):
    _inward(w, [("S17", None, None, "CL17", 3_000_000)], period="2027-10")
    cid = _treaty(w, _xol(w), "S17 endorse XOL")
    _active(cid)
    pv = client.post(f"/contracts/{cid}/endorsement/preview", headers=w["ch"], json={
        "agreed_limits": {}, "effective_from": "2027-09-01",
        "basis": [{"term": "limit", "layer": 0, "value": 2_000_000}]}).json()
    ch = next(x for x in pv["changes"] if x["key"] == "basis:limit:0")
    assert ch["question"] == "Limit — Layer 1" and ch["from"] == "USD 1,000,000"
    assert _endorse(w, cid, "2027-09-01",
                    [{"term": "limit", "layer": 0, "value": 2_000_000}]).status_code == 200
    _approve_endorsements(w, cid)
    v = _versions(w, cid, "limit", 0)[-1]
    assert (v["value"], v["source"], v["note"]) == (2_000_000, "endorsement", "Endorsement 1")
    assert _lines(_statement(w, cid, "2027-10"), "S17")[0]["recovery"] == 2_000_000


def test_s18_an_endorsement_that_changes_nothing_or_cannot_apply_is_refused(w):
    cid = _treaty(w, _qs(w, 40), "S18 endorse refused")
    # A draft is changed by editing its terms, not endorsed.
    assert _endorse(w, cid, "2027-06-01", [{"term": "cession_pct", "value": 30}]).status_code == 409
    _active(cid)
    r = _endorse(w, cid, "2027-06-01", [{"term": "cession_pct", "value": 40}])
    assert r.status_code == 400 and "Nothing has changed" in r.text
    # Outside the treaty's term: refused, and no endorsement left behind.
    r = _endorse(w, cid, "2028-02-01", [{"term": "cession_pct", "value": 30}])
    assert r.status_code == 400 and "expires" in r.text and _endorsements(cid) == 0
    # Two endorsements of the same term on the same date: the second is refused.
    assert _endorse(w, cid, "2027-06-01", [{"term": "cession_pct", "value": 30}]).status_code == 200
    r = _endorse(w, cid, "2027-06-01", [{"term": "cession_pct", "value": 25}])
    assert r.status_code == 400 and _endorsements(cid) == 1


def test_s19_an_endorsement_starts_from_the_term_in_force_on_its_date(w):
    cid = _treaty(w, _qs(w, 40), "S19 endorse after a change")
    _active(cid)
    assert _change(w, cid, "cession_pct", 30, "2027-04-01").status_code == 200
    pv = lambda on: next(x for x in client.post(                 # noqa: E731
        f"/contracts/{cid}/endorsement/preview", headers=w["ch"], json={
            "agreed_limits": {}, "effective_from": on,
            "basis": [{"term": "cession_pct", "value": 20}]}).json()["changes"]
        if x["key"] == "basis:cession_pct:")
    assert pv("2027-03-01")["from"] == "40%"                     # before the change
    assert pv("2027-06-01")["from"] == "30%"                     # after it
    assert _endorse(w, cid, "2027-06-01", [{"term": "cession_pct", "value": 20}]).status_code == 200
    assert [(v["effective_from"], v["value"], v["source"])
            for v in _versions(w, cid, "cession_pct")] == [
        ("2027-01-01", 40, "structure"), ("2027-04-01", 30, "manual"),
        ("2027-06-01", 20, "endorsement")]


# ── negative scenarios: what must be refused, or must not happen ────────────
def _seat(w, role, *, broker=False, tenant=None):
    """A user of `role` — at this carrier, at its broker, or at another carrier."""
    from db import Tenant
    with SessionLocal() as s:
        f = s.query(AppUser).filter(AppUser.role == "kavachio_admin").order_by(AppUser.id).first()
        tid = w["t"]
        if tenant == "other":
            t = Tenant(tenant_name=f"other-{os.urandom(4).hex()}", legal_name="Another Carrier")
            s.add(t); s.commit()
            tid = t.id
        u = AppUser(tenant_id=None if broker else tid,
                    email=f"n-{os.urandom(4).hex()}@x.test", full_name="Nobody",
                    role=role, invited_by_user_id=f.id,
                    broker_party_id=w["b"] if broker else None)
        s.add(u); s.commit()
        if tenant == "other":
            t.owner_user_id = u.id
            s.commit()
        return {"Authorization": f"Bearer {mint_access_token(u.id, None if broker else tid, role)}"}


@pytest.fixture(scope="module")
def qs(w):
    """One quota share treaty the negative scenarios share; nothing they try sticks."""
    return _treaty(w, _qs(w, 40), "N quota share", setup=False)


def _broker_on_programme(w):
    """The broker on the programme — what a broker contract there needs."""
    from db import ProgramBroker
    with SessionLocal() as s:
        if not s.query(ProgramBroker).filter(ProgramBroker.program_id == w["p"],
                                             ProgramBroker.broker_party_id == w["b"]).first():
            s.add(ProgramBroker(program_id=w["p"], broker_party_id=w["b"]))
            s.commit()


def _count(cid):
    with SessionLocal() as s:
        return s.query(TreatyTerm).filter(TreatyTerm.contract_id == cid).count()


def test_n1_nobody_signed_in_gets_nothing(qs):
    assert client.get(f"/contracts/{qs}/basis").status_code == 401
    assert client.post(f"/contracts/{qs}/basis", json={
        "term": "cession_pct", "value": 30, "effective_from": "2027-06-01"}).status_code == 401
    assert client.delete(f"/contracts/{qs}/basis/1").status_code == 401


def test_n2_a_broker_never_sees_or_changes_a_treatys_basis(w, qs):
    for role in ("broker_admin", "operator"):
        h = _seat(w, role, broker=True)
        assert client.get(f"/contracts/{qs}/basis", headers=h).status_code in (403, 404)
        assert client.post(f"/contracts/{qs}/basis", headers=h, json={
            "term": "cession_pct", "value": 30,
            "effective_from": "2027-06-01"}).status_code in (403, 404)
    assert _count(qs) == 0


def test_n3_another_carrier_cannot_reach_this_treaty(w, qs):
    h = _seat(w, "carrier_admin", tenant="other")
    assert client.get(f"/contracts/{qs}/basis", headers=h).status_code in (403, 404)
    assert client.post(f"/contracts/{qs}/basis", headers=h, json={
        "term": "cession_pct", "value": 30, "effective_from": "2027-06-01"}).status_code in (403, 404)
    assert client.post(f"/contracts/{qs}/basis/evaluate", headers=h, json={
        "term": "sliding_scale", "value": {}, "premium": 1, "losses": 1}).status_code in (403, 404)
    assert _count(qs) == 0


def test_n4_no_such_contract_and_not_a_treaty(w):
    assert client.get("/contracts/999999999/basis", headers=w["ch"]).status_code == 404
    _broker_on_programme(w)
    r = client.post("/contracts", headers=w["ch"], json={
        "program_id": w["p"], "contract_type": "insurer_broker",
        "counterparty_party_id": w["b"], "name": "A broker contract",
        "class_of_business": "Property",
        "inception_dt": "2027-01-01", "expiry_dt": "2027-12-31", "create_as": "draft"})
    assert r.status_code == 200, r.text                   # a binder has no calculation basis
    bid = r.json()["id"]
    assert client.get(f"/contracts/{bid}/basis", headers=w["ch"]).status_code in (400, 404)
    assert client.post(f"/contracts/{bid}/basis", headers=w["ch"], json={
        "term": "cession_pct", "value": 30,
        "effective_from": "2027-06-01"}).status_code in (400, 404)
    assert _count(bid) == 0


def test_n5_a_treaty_without_a_structure_has_no_basis_to_change(w):
    r = client.post("/contracts", headers=w["ch"], json={
        "program_id": w["p"], "contract_type": "insurer_reinsurer", "name": "N no structure",
        "counterparty_party_id": w["swiss"],
        "inception_dt": "2027-01-01", "expiry_dt": "2027-12-31", "year_of_account": "2027",
        "notice_period_days": 90, "create_as": "draft"})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    assert client.get(f"/contracts/{cid}/basis", headers=w["ch"]).json()["terms"] == []
    r = client.post(f"/contracts/{cid}/basis", headers=w["ch"], json={
        "term": "cession_pct", "value": 30, "effective_from": "2027-06-01"})
    assert r.status_code == 400 and "structure" in r.text.lower()


@pytest.mark.parametrize("body, status", [
    ({}, 422),                                                        # nothing at all
    ({"term": "cession_pct", "value": 30}, 422),                      # no date
    ({"term": "cession_pct", "effective_from": "2027-06-01"}, 422),   # no value
    ({"term": "cession_pct", "value": None, "effective_from": "2027-06-01"}, 400),
    ({"term": "cession_pct", "value": 30, "effective_from": "2027-13-01"}, 400),  # no 13th month
    ({"term": "cession_pct", "value": 30, "effective_from": "01/06/2027"}, 400),
    ({"term": "cession_pct", "value": 30, "effective_from": "soon"}, 400),
    ({"term": "cession_pct", "value": [30], "effective_from": "2027-06-01"}, 400),
    ({"term": "cession_pct", "value": {"v": 30}, "effective_from": "2027-06-01"}, 400),
    ({"term": "", "value": 30, "effective_from": "2027-06-01"}, 400),
    ({"term": "CESSION_PCT", "value": 30, "effective_from": "2027-06-01"}, 400),
    ({"term": "limit", "layer": 0, "value": 30, "effective_from": "2027-06-01"}, 400),  # an XOL term
])
def test_n6_malformed_changes_are_refused_and_nothing_is_saved(w, qs, body, status):
    before = _count(qs)
    r = client.post(f"/contracts/{qs}/basis", headers=w["ch"], json=body)
    assert r.status_code == status, r.text
    assert _count(qs) == before


@pytest.mark.parametrize("value, says", [
    ({"rate_pct": 20, "deficit_years": -1}, "deficit"),
    ({"rate_pct": 20, "deficit_years": 1.5}, "deficit"),
    ({"rate_pct": 120}, "rate"),
    ({"rate_pct": -10}, "rate"),
    ({"expense_pct": 5}, "rate"),                        # no rate at all
    ("20%", "commission"),                               # not a structure
])
def test_n7_a_profit_commission_that_is_not_sound_is_refused(w, qs, value, says):
    r = client.post(f"/contracts/{qs}/basis", headers=w["ch"], json={
        "term": "profit_commission", "value": value, "effective_from": "2027-06-01"})
    assert r.status_code == 400 and says in r.text.lower(), r.text


@pytest.mark.parametrize("value, says", [
    ({"points": [{"loss_ratio": 50, "rate": 35}, {"loss_ratio": 70, "rate": 25}]}, "provisional"),
    ({"provisional_pct": 130, "points": [{"loss_ratio": 50, "rate": 35},
                                         {"loss_ratio": 70, "rate": 25}]}, "provisional"),
    ({"provisional_pct": 30, "points": []}, "two points"),
    ({"provisional_pct": 30, "points": [{"loss_ratio": 50}, {"rate": 25}]}, "two points"),
    ({"provisional_pct": 30, "points": [{"loss_ratio": "x", "rate": 35},
                                        {"loss_ratio": 70, "rate": 25}]}, "two points"),
    ({"provisional_pct": 30, "points": [{"loss_ratio": 50, "rate": 135},
                                        {"loss_ratio": 70, "rate": 25}]}, "0–100"),
    ({"provisional_pct": 30, "points": [{"loss_ratio": 50, "rate": -5},
                                        {"loss_ratio": 70, "rate": 25}]}, "0–100"),
    ({"provisional_pct": 30, "points": "35 at 50"}, "two points"),
])
def test_n8_a_sliding_scale_that_is_not_sound_is_refused(w, qs, value, says):
    r = client.post(f"/contracts/{qs}/basis", headers=w["ch"], json={
        "term": "sliding_scale", "value": value, "effective_from": "2027-06-01"})
    assert r.status_code == 400 and says in r.text.lower(), r.text


def test_n9_the_agreed_value_cannot_be_removed_only_a_change_can(w, qs):
    # The structure's own version has no id to remove; ids that are not changes 404.
    for bad in (0, -1, 999999999):
        assert client.delete(f"/contracts/{qs}/basis/{bad}", headers=w["ch"]).status_code == 404
    assert client.delete(f"/contracts/{qs}/basis/abc", headers=w["ch"]).status_code == 422
    assert [v["source"] for v in _versions(w, qs, "cession_pct")] == ["structure"]


@pytest.mark.parametrize("body, status", [
    ({"term": "cession_pct", "value": {}, "premium": 1, "losses": 1}, 400),   # not a formula
    ({"term": "sliding_scale", "value": {"provisional_pct": 30}, "premium": 1, "losses": 1}, 400),
    ({"term": "profit_commission", "value": {"rate_pct": 0}, "premium": 1, "losses": 1}, 400),
    ({"term": "sliding_scale", "value": {}}, 422),                           # no figures
    ({"term": "sliding_scale", "premium": "lots", "losses": 1, "value": {}}, 422),
    ({"term": "sliding_scale", "premium": -100, "losses": 50, "value": {
        "provisional_pct": 30, "points": [{"loss_ratio": 50, "rate": 35},
                                          {"loss_ratio": 70, "rate": 25}]}}, 400),
    ({"term": "profit_commission", "premium": 100, "losses": -5, "value": {"rate_pct": 20}}, 400),
])
def test_n10_working_out_a_formula_on_bad_figures_is_refused(w, qs, body, status):
    r = client.post(f"/contracts/{qs}/basis/evaluate", headers=w["ch"], json=body)
    assert r.status_code == status, r.text


def test_n11_a_terminated_treaty_takes_no_change_but_shows_its_terms(w):
    cid = _treaty(w, _qs(w, 40), "N terminated", setup=False)
    with SessionLocal() as s:
        s.get(Contract, cid).lifecycle = "terminated"
        s.commit()
    b = client.get(f"/contracts/{cid}/basis", headers=w["ch"])
    assert b.status_code == 200 and b.json()["can_edit"] is False
    assert _change(w, cid, "cession_pct", 30, "2027-06-01").status_code == 409
    assert _count(cid) == 0


def test_n12_a_change_on_one_layer_does_not_touch_another(w):
    st = _xol(w)
    st["layers"].append({"name": "Layer 2", "terms": {"retention": 2_000_000, "limit": 3_000_000},
                         "shares": [{"reinsurer_party_id": w["swiss"], "share_pct": 100,
                                     "is_lead": True}]})
    cid = _treaty(w, st, "N two layers", setup=False)
    # The same date on two different layers is two terms — both allowed.
    assert _change(w, cid, "limit", 1_500_000, "2027-06-01", layer=0).status_code == 200
    assert _change(w, cid, "limit", 4_000_000, "2027-06-01", layer=1).status_code == 200
    assert [v["value"] for v in _versions(w, cid, "limit", 0)] == [1_000_000, 1_500_000]
    assert [v["value"] for v in _versions(w, cid, "limit", 1)] == [3_000_000, 4_000_000]
    assert [v["value"] for v in _versions(w, cid, "retention", 1)] == [2_000_000]
    # A layer that is not there, by index.
    for bad in (-1, 2, 99):
        assert _change(w, cid, "limit", 5, "2027-07-01", layer=bad).status_code == 400


def test_n13_endorsement_basis_that_cannot_apply_leaves_nothing_behind(w):
    cid = _treaty(w, _qs(w, 40), "N endorse refused", setup=False)
    _active(cid)
    for basis, says in (
            ([{"term": "rate_on_line_pct", "value": 5}], "not a term"),     # not this type's
            ([{"term": "sliding_scale", "value": {"provisional_pct": 30}}], "two points"),
            ([{"term": "profit_commission", "value": {"rate_pct": 0}}], "rate"),
            ([{"term": "cession_pct", "value": 150}], "at most 100"),
            ([{"term": "cession_pct", "value": "lots"}], "number")):
        r = _endorse(w, cid, "2027-06-01", basis)
        assert r.status_code == 400 and says in r.text.lower(), r.text
        pv = client.post(f"/contracts/{cid}/endorsement/preview", headers=w["ch"], json={
            "agreed_limits": {}, "effective_from": "2027-06-01", "basis": basis})
        assert pv.status_code == 400
    assert _endorsements(cid) == 0 and _count(cid) == 0


def test_n14_an_endorsement_without_a_date_cannot_move_the_basis(w):
    cid = _treaty(w, _qs(w, 40), "N endorse no date", setup=False)
    _active(cid)
    r = client.post(f"/contracts/{cid}/endorsement", headers=w["ch"], json={
        "agreed_limits": {}, "basis": [{"term": "cession_pct", "value": 30}]})
    assert r.status_code == 400 and "date" in r.text.lower()
    assert _endorsements(cid) == 0 and _count(cid) == 0


def test_n15_a_broker_contracts_endorsement_ignores_a_basis(w):
    """Only a treaty has a calculation basis — on any other contract the field
    is ignored, never written."""
    _broker_on_programme(w)
    r = client.post("/contracts", headers=w["ch"], json={
        "program_id": w["p"], "contract_type": "insurer_broker",
        "counterparty_party_id": w["b"], "name": "N binder", "class_of_business": "Property",
        "inception_dt": "2027-01-01", "expiry_dt": "2027-12-31", "create_as": "draft"})
    assert r.status_code == 200, r.text
    bid = r.json()["id"]
    _active(bid)
    pv = client.post(f"/contracts/{bid}/endorsement/preview", headers=w["ch"], json={
        "agreed_limits": {}, "effective_from": "2027-06-01",
        "basis": [{"term": "cession_pct", "value": 30}]}).json()
    assert pv["basis"] == [] and not any(c["key"].startswith("basis:") for c in pv["changes"])
    assert _count(bid) == 0


def test_n16_a_removed_change_never_comes_back_into_a_statement(w):
    _inward(w, [("N16", 100_000, 1000, None, None)], period="2027-11")
    cid = _treaty(w, _qs(w, 40), "N removed")
    assert _change(w, cid, "cession_pct", 10, "2027-01-01", approve=False).status_code == 200
    entry = _versions(w, cid, "cession_pct")[-1]["id"]
    assert client.delete(f"/contracts/{cid}/basis/{entry}", headers=w["ch"]).status_code == 200
    # Not in the timeline, not in the statement, not in the endorsement's "from".
    assert [v["value"] for v in _versions(w, cid, "cession_pct")] == [40]
    assert _lines(_statement(w, cid, "2027-11"), "N16")[0]["ceded_pct"] == 40
    _active(cid)
    pv = client.post(f"/contracts/{cid}/endorsement/preview", headers=w["ch"], json={
        "agreed_limits": {}, "effective_from": "2027-06-01",
        "basis": [{"term": "cession_pct", "value": 30}]}).json()
    assert next(c for c in pv["changes"] if c["key"] == "basis:cession_pct:")["from"] == "40%"


def test_n17_a_change_after_the_period_does_not_reach_back_into_it(w):
    _inward(w, [("N17", 100_000, 1000, None, None)], period="2027-02")
    cid = _treaty(w, _qs(w, 40), "N later change")
    assert _change(w, cid, "cession_pct", 10, "2027-03-01").status_code == 200
    assert _change(w, cid, "cession_pct", 5, "2027-12-31").status_code == 200   # the last day
    assert _lines(_statement(w, cid, "2027-02"), "N17")[0]["ceded_pct"] == 40



# ── step 3: the commission terms at creation and by endorsement ─────────────
SCALE = {"provisional_pct": 28, "method": "linear",
         "points": [{"loss_ratio": 50, "rate": 33}, {"loss_ratio": 70, "rate": 23}]}
PC = {"rate_pct": 15, "expense_pct": 4, "deficit_years": 3}


def test_c1_commission_terms_set_when_the_treaty_is_created(w):
    """Given on the structure, they are the value from inception — and the
    sliding scale's provisional rate is what each line is charged."""
    _inward(w, [("C1", 100_000, 1000, None, None)], period="2027-03")
    st = _qs(w, 40)
    st["terms"].update({"sliding_scale": SCALE, "profit_commission": PC})
    cid = _treaty(w, st, "C1 at creation")
    sc = _versions(w, cid, "sliding_scale")
    assert len(sc) == 1 and sc[0]["source"] == "structure" and sc[0]["value"]["provisional_pct"] == 28
    pc = _versions(w, cid, "profit_commission")
    assert pc[0]["value"] == {"rate_pct": 15, "expense_pct": 4, "deficit_years": 3}
    t = client.get(f"/contracts/{cid}/treaty", headers=w["ch"]).json()
    assert t["structure"]["terms"]["profit_commission_pct"] == 15        # quoted by the wording
    facts = {f["label"]: f["value"] for f in t["summary"]["facts"]}
    assert facts["Sliding-scale commission"].startswith("provisional 28%")
    assert facts["Profit commission"] == "15% of profit, 4% expenses, deficit carried 3 yr"
    assert _lines(_statement(w, cid, "2027-03"), "C1")[0]["ceding_commission"] == 112   # 28% of 400


@pytest.mark.parametrize("terms, path", [
    ({"sliding_scale": {"provisional_pct": 28, "points": [{"loss_ratio": 50, "rate": 33}]}},
     "terms.sliding_scale"),
    ({"sliding_scale": "28%"}, "terms.sliding_scale"),
    ({"profit_commission": {"rate_pct": 140}}, "terms.profit_commission"),
])
def test_c2_unsound_commission_terms_stop_the_treaty_being_saved(w, terms, path):
    st = _qs(w, 40)
    st["terms"].update(terms)
    r = client.post("/contracts", headers=w["ch"], json={
        "program_id": w["p"], "contract_type": "insurer_reinsurer", "name": "C2 bad",
        "counterparty_party_id": w["swiss"], "inception_dt": "2027-01-01",
        "expiry_dt": "2027-12-31", "year_of_account": "2027", "notice_period_days": 90,
        "create_as": "draft", "treaty": st})
    assert r.status_code == 400 and path in r.text, r.text


def test_c3_left_empty_they_are_simply_not_agreed(w):
    st = _qs(w, 40)
    st["terms"].update({"sliding_scale": {}, "profit_commission": None})
    cid = _treaty(w, st, "C3 empty", setup=False)
    assert _versions(w, cid, "sliding_scale") == [] and _versions(w, cid, "profit_commission") == []


def test_c4_an_old_profit_commission_rate_is_kept_as_the_profit_commission(w):
    st = _qs(w, 40)
    st["terms"]["profit_commission_pct"] = 12
    cid = _treaty(w, st, "C4 legacy", setup=False)
    assert _versions(w, cid, "profit_commission")[0]["value"] == \
        {"rate_pct": 12, "expense_pct": 0, "deficit_years": 0}


def test_c5_an_endorsement_changes_the_sliding_scale_and_profit_commission(w):
    for per in ("2027-05", "2027-11"):
        _inward(w, [("C5", 100_000, 1000, None, None)], period=per)
    st = _qs(w, 40)
    st["terms"].update({"sliding_scale": SCALE, "profit_commission": PC})
    cid = _treaty(w, st, "C5 endorse commissions")
    _active(cid)
    new_scale = {**SCALE, "provisional_pct": 20}
    new_pc = {**PC, "rate_pct": 25}
    pv = client.post(f"/contracts/{cid}/endorsement/preview", headers=w["ch"], json={
        "agreed_limits": {}, "effective_from": "2027-09-01",
        "basis": [{"term": "sliding_scale", "value": new_scale},
                  {"term": "profit_commission", "value": new_pc}]}).json()
    ch = {c["key"]: c for c in pv["changes"]}
    assert ch["basis:sliding_scale:"]["from"].startswith("provisional 28%")
    assert ch["basis:sliding_scale:"]["to"].startswith("provisional 20%")
    assert ch["basis:profit_commission:"]["to"].startswith("25% of profit")
    # The same terms again is no change.
    same = client.post(f"/contracts/{cid}/endorsement/preview", headers=w["ch"], json={
        "agreed_limits": {}, "effective_from": "2027-09-01",
        "basis": [{"term": "profit_commission", "value": PC}]}).json()
    assert not any(c["key"].startswith("basis:") for c in same["changes"])

    assert _endorse(w, cid, "2027-09-01", [{"term": "sliding_scale", "value": new_scale},
                                            {"term": "profit_commission", "value": new_pc}]
                    ).status_code == 200
    _approve_endorsements(w, cid)
    assert [v["source"] for v in _versions(w, cid, "sliding_scale")] == ["structure", "endorsement"]
    assert _versions(w, cid, "profit_commission")[-1]["value"]["rate_pct"] == 25
    comm = lambda per: _lines(_statement(w, cid, per), "C5")[0]["ceding_commission"]  # noqa: E731
    assert (comm("2027-05"), comm("2027-11")) == (112, 80)          # 28% → 20% of 400


# ── the wording: what Kavachio writes, and what it reads ────────────────────
def _commission_clause(st):
    import treaty_types as tt
    sec = next(x for x in tt.build_sections(tt.validate(st, strict=False))
               if "Commission" in (x.get("title") or ""))
    return sec["body"]


def test_w1_the_written_wording_states_the_sliding_scale_and_profit_commission(w):
    st = _qs(w, 40)
    st["terms"].update({"sliding_scale": SCALE, "profit_commission": PC})
    body = _commission_clause(st)
    assert "provisional commission of 28%" in body
    assert "at a loss ratio of 50% or less, 33%" in body
    assert "at a loss ratio of 70% or more, 23%" in body
    assert "slides proportionately" in body
    # The provisional rate is the commission — no second, fixed rate stated.
    assert "ceding commission of 30%" not in body
    assert "profit commission of 15%" in body
    assert "management expenses of 4% of premium ceded" in body
    assert "any deficit brought forward" in body and "no more than 3 years" in body


def test_w2_the_wording_says_only_what_was_agreed(w):
    st = _qs(w, 40)                                          # 30% ceding, no scale, no PC
    body = _commission_clause(st)
    assert "ceding commission of 30%" in body
    assert "provisional" not in body and "profit commission" not in body
    st["terms"].update({"sliding_scale": {**SCALE, "method": "step"},
                        "profit_commission": {"rate_pct": 10}})
    body = _commission_clause(st)
    assert "the rate at the lower point" in body
    assert "profit commission of 10%" in body
    assert "management expenses" not in body and "deficit" not in body


def test_w3_the_reader_takes_the_commission_terms_from_a_signed_wording():
    import json
    import treaty_reader
    seen = {}

    def call(prompt):
        seen["prompt"] = prompt
        return json.dumps({"treaty_type": "quota_share", "terms": {
            "currency": "USD", "cession_pct": 40, "ceding_commission_pct": 27.5,
            "sliding_scale": {"provisional_pct": 27.5, "method": "linear", "points": [
                {"loss_ratio": 65, "rate": 22.5}, {"loss_ratio": 45, "rate": 32.5}]},
            "profit_commission": {"rate_pct": 20, "expense_pct": 5, "deficit_years": 2}},
            "layers": [{"name": "QS", "terms": {}, "shares": []}]})
    got = treaty_reader.read_treaty("Treaty wording…", [], call=call)
    assert "terms.sliding_scale" in seen["prompt"] and "terms.profit_commission" in seen["prompt"]
    terms = got["treaty"]["terms"]
    assert [p["loss_ratio"] for p in terms["sliding_scale"]["points"]] == [45, 65]   # in order
    assert terms["profit_commission"] == {"rate_pct": 20, "expense_pct": 5, "deficit_years": 2}
    assert terms["profit_commission_pct"] == 20


@pytest.mark.parametrize("bad", [
    {"sliding_scale": {"provisional_pct": 27.5, "points": [{"loss_ratio": 45, "rate": 32.5}]}},
    {"sliding_scale": "27.5% provisional, sliding 22.5–32.5%"},
    {"profit_commission": {"rate_pct": "twenty"}},
    {"profit_commission": 20},
])
def test_w4_a_commission_term_read_wrongly_is_dropped_never_crashes(bad):
    """A wording read badly leaves that term for the carrier to enter — the
    rest of the reading stands."""
    import json
    import treaty_reader
    got = treaty_reader.read_treaty("…", [], call=lambda p: json.dumps({
        "treaty_type": "quota_share",
        "terms": {"currency": "USD", "cession_pct": 40, **bad},
        "layers": [{"name": "QS", "terms": {}, "shares": []}]}))
    terms = got["treaty"]["terms"]
    assert terms["cession_pct"] == 40
    assert "sliding_scale" not in terms and "profit_commission" not in terms


# ── an endorsement uploaded on a treaty: read, reviewed, applied ────────────
def _pdf(*pages):
    import fitz
    doc = fitz.open()
    for t in pages:
        doc.new_page().insert_text((72, 72), t)
    return doc.tobytes()


def _upload_endorsement(w, cid, data=None, name="Endorsement 2.pdf"):
    r = client.post(f"/contracts/{cid}/documents", headers=w["ch"],
                    files={"file": (name, data or _pdf(
                        "Endorsement No. 2 to the Quota Share Treaty.",
                        "With effect from 1 July 2027 the cession is amended to 30%. "
                        "Clause 9 Exclusions: war and terrorism are excluded."), "application/pdf")},
                    data={"kind": "endorsement"})
    assert r.status_code == 200, r.text
    return r.json()["document"]["id"]


READ_ANSWER = {
    "endorsement_number": "2", "effective_date": "2027-07-01",
    "changes": [{"term": "cession_pct", "layer": None, "value": 30, "page": 2,
                 "quote": "the cession is amended to 30%"},
                {"term": "rate_on_line_pct", "layer": None, "value": 5, "page": 2},  # not a QS term
                {"term": "cession_pct", "layer": None, "value": 140, "page": 2}],   # not sound
    "other_changes": [{"text": "The notice of cancellation is now 60 days.", "page": 2}],
    "clauses": [{"title": "Clause 9 Exclusions", "text": "War and terrorism are excluded.", "page": 2},
                {"title": "Empty", "text": "  "}],
}


def _read(w, cid, doc_id, monkeypatch, answer=None):
    import json
    import treaty_endorsement as te
    real = te.read
    monkeypatch.setattr(te, "read", lambda s, c, d, **kw: real(
        s, c, d, call=lambda p: json.dumps(answer or READ_ANSWER)))
    return client.post(f"/contracts/{cid}/endorsements/{doc_id}/read", headers=w["ch"])


def _clauses(w, cid):
    r = client.get(f"/programs/{w['p']}/contracts/{cid}", headers=w["ch"]).json()
    return [c for c in r.get("clauses") or [] if (c.get("section_header") or "").startswith("Endorsement")]


def test_e1_an_uploaded_endorsement_is_read_and_nothing_applied_until_confirmed(w, monkeypatch):
    cid = _treaty(w, _qs(w, 40), "E1 read", setup=False)
    doc = _upload_endorsement(w, cid)
    e = client.get(f"/contracts/{cid}/endorsements/{doc}", headers=w["ch"]).json()
    assert e["reading"] is None and e["applied"] == []
    assert e["document"]["validation"]["pages"] == 2
    assert next(t for t in e["terms"] if t["term"] == "cession_pct")["value_before"] == 40
    assert client.get(f"/contracts/{cid}/documents/{doc}/pages/2",
                      headers=w["ch"]).headers["content-type"] == "image/png"

    r = _read(w, cid, doc, monkeypatch)
    assert r.status_code == 200, r.text
    rd = r.json()["reading"]
    assert rd["effective_date"] == "2027-07-01" and rd["endorsement_number"] == "2"
    assert [(c["term"], c["value"], c["page"]) for c in rd["changes"]] == [("cession_pct", 30, 2)]
    others = " ".join(o["text"] for o in rd["other_changes"])
    assert "rate_on_line_pct" in others and "140" in others and "60 days" in others
    assert [c["title"] for c in rd["clauses"]] == ["Clause 9 Exclusions"]
    # Read, not applied: the basis and the clauses are as they were.
    assert [v["value"] for v in _versions(w, cid, "cession_pct")] == [40]
    assert _clauses(w, cid) == []


def test_e2_confirming_applies_its_terms_from_its_date_and_its_clauses(w, monkeypatch):
    for per in ("2027-06", "2027-08"):
        _inward(w, [("E2", 100_000, 1000, None, None)], period=per)
    cid = _treaty(w, _qs(w, 40), "E2 confirm")
    doc = _upload_endorsement(w, cid)
    rd = _read(w, cid, doc, monkeypatch).json()["reading"]
    r = client.post(f"/contracts/{cid}/endorsements/{doc}/confirm", headers=w["ch"], json={
        "effective_date": rd["effective_date"], "endorsement_number": "2",
        "changes": rd["changes"], "clauses": rd["clauses"]})
    assert r.status_code == 200, r.text
    out = r.json()
    _approve_endorsements(w, cid)
    assert out["document"]["effective_from"] == "2027-07-01"
    assert [(a["term"], a["value"], a["effective_from"]) for a in out["applied"]] == \
        [("cession_pct", 30, "2027-07-01")]
    v = _versions(w, cid, "cession_pct")[-1]
    assert (v["source"], v["document_id"], v["note"]) == ("endorsement", doc, "Endorsement 2, page 2")
    cl = _clauses(w, cid)
    assert len(cl) == 1 and cl[0]["title"] == "Clause 9 Exclusions" and cl[0]["page_number"] == 2
    assert cl[0]["section_header"] == "Endorsement 2 — Clause 9 Exclusions"
    pct = lambda per: _lines(_statement(w, cid, per), "E2")[0]["ceded_pct"]   # noqa: E731
    assert (pct("2027-06"), pct("2027-08")) == (40, 30)
    docs = client.get(f"/contracts/{cid}", headers=w["ch"]).json()["documents"]
    assert next(x for x in docs if x["id"] == doc)["endorsement_applied"] is True


def test_e3_confirming_again_replaces_what_it_applied(w, monkeypatch):
    cid = _treaty(w, _qs(w, 40), "E3 reconfirm", setup=False)
    doc = _upload_endorsement(w, cid)
    rd = _read(w, cid, doc, monkeypatch).json()["reading"]
    url = f"/contracts/{cid}/endorsements/{doc}/confirm"
    assert client.post(url, headers=w["ch"], json={
        "effective_date": "2027-07-01", "changes": rd["changes"], "clauses": rd["clauses"]}).status_code == 200
    # Corrected on review: 25%, from 1 August, the commission too, a different clause.
    r = client.post(url, headers=w["ch"], json={
        "effective_date": "2027-08-01",
        "changes": [{"term": "cession_pct", "value": 25},
                    {"term": "ceding_commission_pct", "value": 27.5}],
        "clauses": [{"title": "Clause 9", "text": "War, terrorism and cyber are excluded."}]})
    assert r.status_code == 200, r.text
    assert [(v["effective_from"], v["value"]) for v in _versions(w, cid, "cession_pct")] == \
        [("2027-01-01", 40), ("2027-08-01", 25)]                       # the 30% is gone
    assert _versions(w, cid, "ceding_commission_pct")[-1]["value"] == 27.5
    cl = _clauses(w, cid)
    assert len(cl) == 1 and "cyber" in cl[0]["text"]
    # Confirmed with no changes and no clauses: it withdraws what it applied.
    assert client.post(url, headers=w["ch"], json={"changes": [], "clauses": []}).status_code == 200
    assert [v["value"] for v in _versions(w, cid, "cession_pct")] == [40] and _clauses(w, cid) == []


@pytest.mark.parametrize("body, says", [
    ({"changes": [{"term": "cession_pct", "value": 30}]}, "date"),                    # no date
    ({"effective_date": "2028-03-01", "changes": [{"term": "cession_pct", "value": 30}]}, "expires"),
    ({"effective_date": "2027-07-01", "changes": [{"term": "cession_pct", "value": 30},
                                                  {"term": "cession_pct", "value": 25}]}, "each term once"),
    ({"effective_date": "2027-07-01", "changes": [{"term": "limit", "layer": 0, "value": 5}]}, "not a term"),
    ({"effective_date": "2027-07-01", "changes": [{"term": "cession_pct", "value": "lots"}]}, "number"),
    ({"effective_date": "2027-07-01", "changes": [{"term": "sliding_scale",
                                                   "value": {"provisional_pct": 30}}]}, "two points"),
])
def test_e4_a_confirmation_that_cannot_apply_changes_nothing(w, monkeypatch, body, says):
    cid = _treaty(w, _qs(w, 40), f"E4 {says}", setup=False)
    doc = _upload_endorsement(w, cid)
    good = {"effective_date": "2027-06-01", "changes": [{"term": "cession_pct", "value": 35}],
            "clauses": [{"title": "A", "text": "Kept."}]}
    url = f"/contracts/{cid}/endorsements/{doc}/confirm"
    assert client.post(url, headers=w["ch"], json=good).status_code == 200
    r = client.post(url, headers=w["ch"], json={"clauses": [], **body})
    assert r.status_code == 400 and says in r.text.lower(), r.text
    # The earlier confirmation stands, untouched.
    assert [v["value"] for v in _versions(w, cid, "cession_pct")] == [40, 35]
    assert len(_clauses(w, cid)) == 1


def test_e5_what_is_not_an_endorsement_of_this_treaty_is_refused(w, monkeypatch):
    cid = _treaty(w, _qs(w, 40), "E5 refused", setup=False)
    other = _treaty(w, _qs(w, 40), "E5 other", setup=False)
    doc = _upload_endorsement(w, cid)
    for bad in (f"/contracts/{other}/endorsements/{doc}", f"/contracts/{cid}/endorsements/999999999"):
        assert client.get(bad, headers=w["ch"]).status_code == 404
        assert client.post(bad + "/confirm", headers=w["ch"], json={}).status_code == 404
    ref = client.post(f"/contracts/{cid}/documents", headers=w["ch"],
                      files={"file": ("ref.pdf", _pdf("A reference"), "application/pdf")},
                      data={"kind": "reference"}).json()["document"]["id"]
    assert client.get(f"/contracts/{cid}/endorsements/{ref}", headers=w["ch"]).status_code == 404
    # A scan with no text is not read — its changes are entered by hand.
    scan = _upload_endorsement(w, cid, data=_pdf(""), name="scan.pdf")
    assert _read(w, cid, scan, monkeypatch).status_code == 409
    # A terminated treaty takes no endorsement.
    with SessionLocal() as s:
        s.get(Contract, cid).lifecycle = "terminated"
        s.commit()
    assert client.post(f"/contracts/{cid}/endorsements/{doc}/confirm", headers=w["ch"], json={
        "effective_date": "2027-07-01", "changes": []}).status_code == 409


def test_v1_a_word_document_is_shown_in_the_page_as_a_pdf(w):
    """View converts a Word file to a PDF on the server, so it reads in the
    page; a file that converts to nothing is refused, and download stays."""
    import io
    import docx
    cid = _treaty(w, _qs(w, 40), "V1 view word", setup=False)
    d = docx.Document()
    d.add_paragraph("Endorsement 002 — the inception date is amended to 1 September 2027.")
    buf = io.BytesIO()
    d.save(buf)
    up = client.post(f"/contracts/{cid}/documents", headers=w["ch"],
                     files={"file": ("Endst 002.docx", buf.getvalue(),
                                     "application/vnd.openxmlformats-officedocument."
                                     "wordprocessingml.document")},
                     data={"kind": "endorsement"})
    assert up.status_code == 200, up.text
    doc = up.json()["document"]["id"]
    r = client.get(f"/contracts/{cid}/documents/{doc}/view", headers=w["ch"])
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
    assert r.headers["content-disposition"].startswith("inline")
    assert client.get(f"/contracts/{cid}/documents/{doc}/download",
                      headers=w["ch"]).content == buf.getvalue()           # the file as it is
    bad = client.post(f"/contracts/{cid}/documents", headers=w["ch"],
                      files={"file": ("notes.docx", b"not a word file", "application/octet-stream")},
                      data={"kind": "reference"}).json()["document"]["id"]
    assert client.get(f"/contracts/{cid}/documents/{bad}/view", headers=w["ch"]).status_code == 409
    assert client.get(f"/contracts/{cid}/documents/999999999/view", headers=w["ch"]).status_code == 404
    assert client.get(f"/contracts/{cid}/documents/{doc}/view").status_code == 401


# ── rules read from an endorsement's clauses ────────────────────────────────
ENDO_RULES = [
    {"id": "wording_1_sum_insured_max", "label": "At most 50,000 any one risk", "kind": "max",
     "subject": "sum_insured", "params": {"value": 50_000}, "severity": "critical",
     "enabled": True, "source": "wording", "clause": "Maximum any one risk 50,000."},
    {"id": "wording_2_territory_excluded", "label": "Cuba is excluded", "kind": "excluded",
     "subject": "territory", "params": {"values": ["Cuba"]}, "severity": "critical",
     "enabled": True, "source": "wording", "clause": "Risks in Cuba are excluded."},
]


def _apply(w, cid, doc, on="2027-07-01", clauses=None, changes=None):
    return client.post(f"/contracts/{cid}/endorsements/{doc}/confirm", headers=w["ch"], json={
        "effective_date": on, "endorsement_number": "3", "changes": changes or [],
        "clauses": clauses if clauses is not None else [
            {"title": "Clause 12 Limits", "text": "Maximum any one risk 50,000."},
            {"title": "Clause 13 Territory", "text": "Risks in Cuba are excluded."}]})


def _rules(w, cid):
    return client.get(f"/reinsurance/setups/{cid}", headers=w["ch"]).json()["setup"]["rules"]


def _checks(cb):
    return {c["id"]: c for c in cb["checks"]}


def test_r1_an_endorsements_clauses_are_read_for_rules_from_its_date(w, monkeypatch):
    import treaty_rules as trr
    seen = {}
    monkeypatch.setattr(trr, "extract", lambda text, **kw: seen.setdefault("text", text) and
                        [dict(r) for r in ENDO_RULES])
    for per in ("2027-05", "2027-09"):
        _inward(w, [("R1", 100_000, 1000, None, None)], period=per)
    cid = _treaty(w, _qs(w, 40), "R1 endorsement rules")
    doc = _upload_endorsement(w, cid)
    r = _apply(w, cid, doc)
    assert r.status_code == 200, r.text
    _approve_endorsements(w, cid)
    assert "Clause 12 Limits" in seen["text"] and "Cuba" in seen["text"]   # read from its clauses
    rules = r.json()["rules"]
    assert [x["id"] for x in rules] == [f"endorsement{doc}_1_sum_insured_max",
                                        f"endorsement{doc}_2_territory_excluded"]
    assert {(x["source"], x["endorsement"], x["effective_from"]) for x in rules} == \
        {("endorsement", "Endorsement 3", "2027-07-01")}
    # On the treaty's rules, with the rest.
    mine = {x["id"]: x for x in _rules(w, cid) if x["source"] == "endorsement"}
    assert set(mine) == {x["id"] for x in rules}
    # A statement BEFORE its date is not checked against it; one after is.
    rid = f"endorsement{doc}_1_sum_insured_max"
    assert rid not in _checks(_statement(w, cid, "2027-05"))
    sep = _checks(_statement(w, cid, "2027-09"))
    assert sep[rid]["severity"] == "critical" and any(
        it["policy"] == "R1" for it in sep[rid]["items"])


def test_r2_the_carriers_severity_on_an_endorsement_rule_is_kept(w, monkeypatch):
    import treaty_rules as trr
    monkeypatch.setattr(trr, "extract", lambda text, **kw: [dict(ENDO_RULES[0])])
    cid = _treaty(w, _qs(w, 40), "R2 severity")
    doc = _upload_endorsement(w, cid)
    assert _apply(w, cid, doc).status_code == 200
    _approve_endorsements(w, cid)
    d = client.get(f"/reinsurance/setups/{cid}", headers=w["ch"]).json()
    rules = [{**x, "severity": "warning"} if x["source"] == "endorsement" else x
             for x in d["setup"]["rules"]]
    base = {k: v for k, v in d["setup"].items()
            if k in ("frequency", "due_days", "program_ids", "columns", "layout",
                     "input_layout", "file_format", "recipients", "cc", "send_mode")}
    assert client.put(f"/reinsurance/setups/{cid}", headers=w["ch"],
                      json={**base, "rules": rules}).status_code == 200
    got = next(x for x in _rules(w, cid) if x["source"] == "endorsement")
    assert got["severity"] == "warning" and got["effective_from"] == "2027-07-01"


def test_r3_applying_again_replaces_its_rules_and_a_failed_read_still_applies(w, monkeypatch):
    import treaty_rules as trr
    cid = _treaty(w, _qs(w, 40), "R3 replace", setup=False)
    doc = _upload_endorsement(w, cid)
    monkeypatch.setattr(trr, "extract", lambda text, **kw: [dict(r) for r in ENDO_RULES])
    assert len(_apply(w, cid, doc).json()["rules"]) == 2
    monkeypatch.setattr(trr, "extract", lambda text, **kw: [dict(ENDO_RULES[1])])
    out = _apply(w, cid, doc, clauses=[{"title": "Territory", "text": "Cuba excluded."}]).json()
    assert [x["subject"] for x in out["rules"]] == ["territory"]
    # No clauses: no rules.
    assert _apply(w, cid, doc, clauses=[]).json()["rules"] == []

    def boom(text, **kw):
        raise RuntimeError("model down")
    monkeypatch.setattr(trr, "extract", boom)
    r = _apply(w, cid, doc, changes=[{"term": "cession_pct", "value": 30}])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["rules"] == [] and "could not be read" in out["rules_error"]
    assert [a["value"] for a in out["applied"]] == [30]            # the terms still applied


def test_r4_a_retired_endorsements_rules_stop(w, monkeypatch):
    import treaty_rules as trr
    monkeypatch.setattr(trr, "extract", lambda text, **kw: [dict(ENDO_RULES[1])])
    cid = _treaty(w, _qs(w, 40), "R4 retired")
    doc = _upload_endorsement(w, cid)
    assert _apply(w, cid, doc).status_code == 200
    _approve_endorsements(w, cid)
    assert any(x["source"] == "endorsement" for x in _rules(w, cid))
    assert client.delete(f"/contracts/{cid}/documents/{doc}",
                         headers=w["ch"]).status_code in (200, 204)
    assert not any(x["source"] == "endorsement" for x in _rules(w, cid))


def test_r5_rules_in_force_on_a_date():
    import treaty_rules as trr
    rules = [{"id": "t", "source": "terms"},
             {"id": "e1", "source": "endorsement", "effective_from": "2027-07-01"},
             {"id": "e2", "source": "endorsement", "effective_from": None}]
    assert [r["id"] for r in trr.in_force_on(rules, "2027-06-01")] == ["t", "e2"]
    assert [r["id"] for r in trr.in_force_on(rules, "2027-07-01")] == ["t", "e1", "e2"]
    assert [r["id"] for r in trr.in_force_on(rules, None)] == ["t", "e1", "e2"]



# ── an endorsement is used only once every reinsurer on the treaty approved it ──
def _sent_links(w, cid, doc, monkeypatch):
    import treaty_routes
    got = []
    monkeypatch.setattr(treaty_routes, "_send_link_email",
                        lambda s, link, c, token, code, **kw: got.append((link.party_id, token, code)) or True)
    r = client.post(f"/contracts/{cid}/endorsements/{doc}/send-for-approval", headers=w["ch"])
    assert r.status_code == 200, r.text
    out = {}
    for pid, token, code in got:
        v = client.post(f"/treaty-view/{token}/verify", json={"code": code})
        out.setdefault(pid, (token, {"X-Treaty-Session": v.json()["session"]}))
    return r.json(), out


def test_a1_an_endorsement_is_used_only_once_approved_and_signed_by_all(w, monkeypatch):
    for per in ("2027-08",):
        _inward(w, [("A1", 100_000, 1000, None, None)], period=per)
    cid = _treaty(w, _qs(w, 40), "A1 approval")
    _active(cid)
    r = _endorse(w, cid, "2027-07-01", [{"term": "cession_pct", "value": 30}])
    doc = r.json()["endorsement"]["document_id"]
    e = client.get(f"/contracts/{cid}/endorsements/{doc}", headers=w["ch"]).json()
    assert e["approval"]["status"] == "draft"
    # Applied but not approved: listed as waiting, not used.
    v = _versions(w, cid, "cession_pct")[-1]
    assert v["value"] == 30 and v["pending"] is True
    assert _lines(_statement(w, cid, "2027-08"), "A1")[0]["ceded_pct"] == 40

    out, links = _sent_links(w, cid, doc, monkeypatch)
    assert out["approval"]["status"] == "pending" and out["approval"]["total"] == 2
    assert set(links) == {w["swiss"], w["munich"]}
    # The reinsurer sees it on its link, with what it changes.
    t, h = links[w["swiss"]]
    view = client.get(f"/treaty-view/{t}", headers=h).json()
    mine = next(x for x in view["endorsements"] if x["id"] == doc)
    assert mine["can_answer"] and mine["changes"] and mine["effective_from"] == "2027-07-01"

    assert client.post(f"/treaty-view/{t}/endorsements/{doc}/approve", headers=h,
                       json={"signer_name": "Sam"}).json()["in_force"] is False
    assert _versions(w, cid, "cession_pct")[-1]["pending"] is True          # one of two
    t2, h2 = links[w["munich"]]
    r = client.post(f"/treaty-view/{t2}/endorsements/{doc}/approve", headers=h2,
                    json={"signer_name": "Mia"}).json()
    assert r["agreed"] is True and r["in_force"] is False
    e = client.get(f"/contracts/{cid}/endorsements/{doc}", headers=w["ch"]).json()
    assert e["approval"]["status"] == "approved" and e["approval"]["approved"] == 2
    # Approved is not in force: the carrier signs, then every reinsurer.
    assert _versions(w, cid, "cession_pct")[-1]["pending"] is True
    assert client.post(f"/contracts/{cid}/endorsements/{doc}/send-for-approval",
                       headers=w["ch"]).status_code == 409
    # A reinsurer cannot sign before the carrier has.
    assert client.post(f"/treaty-view/{t}/endorsements/{doc}/sign", headers=h,
                       json={"signer_name": "Sam", "consent": True}).status_code == 409
    import treaty_routes
    sign_links = []
    monkeypatch.setattr(treaty_routes, "_send_link_email",
                        lambda s, link, c, token, code, **kw:
                        sign_links.append((link.party_id, token, code, kw.get("kind"))) or True)
    r = client.post(f"/contracts/{cid}/endorsements/{doc}/sign", headers=w["ch"],
                    json={"signer_name": "Dana"})
    assert r.status_code == 200, r.text
    assert r.json()["approval"]["status"] == "carrier_signed"
    assert {k for *_, k in sign_links} == {"endorsement_sign"}
    assert {pid for pid, *_ in sign_links} == {w["swiss"], w["munich"]}
    signed = {}
    for pid, token, code, _k in sign_links:
        v = client.post(f"/treaty-view/{token}/verify", json={"code": code})
        signed.setdefault(pid, (token, {"X-Treaty-Session": v.json()["session"]}))
    t, h = signed[w["swiss"]]
    view = client.get(f"/treaty-view/{t}", headers=h).json()
    assert next(x for x in view["endorsements"] if x["id"] == doc)["can_sign"] is True
    assert client.post(f"/treaty-view/{t}/endorsements/{doc}/sign", headers=h,
                       json={"signer_name": "Sam"}).status_code == 400       # no consent
    assert client.post(f"/treaty-view/{t}/endorsements/{doc}/sign", headers=h,
                       json={"signer_name": "Sam", "consent": True}).json()["in_force"] is False
    assert _versions(w, cid, "cession_pct")[-1]["pending"] is True           # one of two
    t2, h2 = signed[w["munich"]]
    assert client.post(f"/treaty-view/{t2}/endorsements/{doc}/sign", headers=h2,
                       json={"signer_name": "Mia", "consent": True}).json()["in_force"] is True
    assert _versions(w, cid, "cession_pct")[-1]["pending"] is False
    e = client.get(f"/contracts/{cid}/endorsements/{doc}", headers=w["ch"]).json()
    assert e["approval"]["status"] == "in_force" and e["approval"]["signed"] == 2
    assert _lines(_statement(w, cid, "2027-08"), "A1")[0]["ceded_pct"] == 30


def test_a2_a_reinsurer_asking_for_changes_holds_the_endorsement(w, monkeypatch):
    cid = _treaty(w, _qs(w, 40), "A2 changes", setup=False)
    _active(cid)
    doc = _endorse(w, cid, "2027-07-01", [{"term": "cession_pct", "value": 30}]
                   ).json()["endorsement"]["document_id"]
    _out, links = _sent_links(w, cid, doc, monkeypatch)
    t, h = links[w["munich"]]
    assert client.post(f"/treaty-view/{t}/endorsements/{doc}/request-changes", headers=h,
                       json={"note": ""}).status_code == 400
    r = client.post(f"/treaty-view/{t}/endorsements/{doc}/request-changes", headers=h,
                    json={"note": "Make it 35%"})
    assert r.status_code == 200, r.text
    e = client.get(f"/contracts/{cid}/endorsements/{doc}", headers=w["ch"]).json()
    assert e["approval"]["status"] == "changes_requested"
    munich = next(p for p in e["approval"]["parties"] if p["party_id"] == w["munich"])
    assert munich["note"] == "Make it 35%"
    # The other reinsurer can no longer approve it as it stands.
    t2, h2 = links[w["swiss"]]
    assert client.post(f"/treaty-view/{t2}/endorsements/{doc}/approve", headers=h2,
                       json={"signer_name": "Sam"}).status_code == 409
    assert _versions(w, cid, "cession_pct")[-1]["pending"] is True
    # Sent again, it waits for fresh answers from everyone.
    out, _ = _sent_links(w, cid, doc, monkeypatch)
    assert out["approval"]["status"] == "pending" and out["approval"]["approved"] == 0


def test_a3_changing_an_applied_endorsement_needs_approving_again(w, monkeypatch):
    cid = _treaty(w, _qs(w, 40), "A3 reapply", setup=False)
    doc = _upload_endorsement(w, cid)
    rd = _read(w, cid, doc, monkeypatch).json()["reading"]
    url = f"/contracts/{cid}/endorsements/{doc}/confirm"
    assert client.post(url, headers=w["ch"], json={
        "effective_date": "2027-07-01", "changes": rd["changes"], "clauses": rd["clauses"]}).status_code == 200
    _approve_endorsements(w, cid)
    assert _versions(w, cid, "cession_pct")[-1]["pending"] is False
    assert client.post(url, headers=w["ch"], json={
        "effective_date": "2027-07-01", "changes": [{"term": "cession_pct", "value": 25}],
        "clauses": []}).status_code == 200
    e = client.get(f"/contracts/{cid}/endorsements/{doc}", headers=w["ch"]).json()
    assert e["approval"]["status"] == "draft"
    assert _versions(w, cid, "cession_pct")[-1]["pending"] is True


def test_a4_a_draft_endorsement_is_not_sent_before_it_is_applied(w, monkeypatch):
    cid = _treaty(w, _qs(w, 40), "A4 unread", setup=False)
    doc = _upload_endorsement(w, cid)
    assert client.post(f"/contracts/{cid}/endorsements/{doc}/send-for-approval",
                       headers=w["ch"]).status_code == 409


# ── a change entered on the basis: draft → sent → approved by every reinsurer ─
def _send_basis(w, cid):
    got = []
    real = treaty_routes._send_link_email
    treaty_routes._send_link_email = lambda s, link, c, token, code, **kw: (
        got.append((token, code)) or True)
    try:
        r = client.post(f"/contracts/{cid}/basis/send-for-approval", headers=w["ch"])
    finally:
        treaty_routes._send_link_email = real
    sessions = []
    for token, code in got:
        v = client.post(f"/treaty-view/{token}/verify", json={"code": code})
        sessions.append((token, {"X-Treaty-Session": v.json()["session"]}))
    return r, sessions


def test_b1_a_change_is_a_draft_and_unused_until_every_reinsurer_approves(w):
    _inward(w, [("B1", 100_000, 1000, None, None)], period="2027-09")
    cid = _treaty(w, _qs(w, 40), "B1 approval")
    assert _change(w, cid, "cession_pct", 25, "2027-08-01", approve=False).status_code == 200
    v = _versions(w, cid, "cession_pct")[-1]
    assert v["approval"] == "draft" and v["pending"]
    assert _lines(_statement(w, cid, "2027-09"), "B1")[0]["ceded_pct"] == 40
    b = client.get(f"/contracts/{cid}/basis", headers=w["ch"]).json()
    assert b["approval"]["draft"] == 1 and b["approval"]["round"] is None

    r, sessions = _send_basis(w, cid)
    assert r.status_code == 200, r.text and sessions
    rd = r.json()["approval"]["round"]
    assert rd["status"] == "pending" and [c["value"] for c in rd["changes"]] == ["25%"]
    assert _versions(w, cid, "cession_pct")[-1]["approval"] == "pending"
    # The reinsurer sees what it is asked to approve.
    view = client.get(f"/treaty-view/{sessions[0][0]}", headers=sessions[0][1]).json()
    assert view["basis_changes"][0]["can_answer"] and \
        view["basis_changes"][0]["changes"][0]["value"] == "25%"
    # Still not used while any reinsurer has not answered.
    assert _lines(_statement(w, cid, "2027-09"), "B1")[0]["ceded_pct"] == 40
    for token, h in sessions:
        a = client.post(f"/treaty-view/{token}/basis/{rd['id']}/approve", headers=h,
                        json={"signer_name": "Approver"})
        assert a.status_code in (200, 409), a.text
    assert _versions(w, cid, "cession_pct")[-1]["approval"] == "approved"
    assert _lines(_statement(w, cid, "2027-09"), "B1")[0]["ceded_pct"] == 25


def test_b2_a_request_for_changes_sends_it_back_and_send_again_starts_over(w):
    cid = _treaty(w, _qs(w, 40), "B2 changes", setup=False)
    _change(w, cid, "cession_pct", 30, "2027-06-01", approve=False)
    r, sessions = _send_basis(w, cid)
    rid = r.json()["approval"]["round"]["id"]
    token, h = sessions[0]
    assert client.post(f"/treaty-view/{token}/basis/{rid}/request-changes", headers=h,
                       json={"note": ""}).status_code == 400
    a = client.post(f"/treaty-view/{token}/basis/{rid}/request-changes", headers=h,
                    json={"note": "35%, not 30%"})
    assert a.status_code == 200, a.text
    b = client.get(f"/contracts/{cid}/basis", headers=w["ch"]).json()
    assert b["approval"]["round"]["status"] == "changes_requested"
    assert _versions(w, cid, "cession_pct")[-1]["approval"] == "changes_requested"
    # No more answers to a request sent back.
    assert client.post(f"/treaty-view/{token}/basis/{rid}/approve", headers=h,
                       json={"signer_name": "X"}).status_code == 409
    # Corrected and sent again: a new request, with no answers yet.
    entry = _versions(w, cid, "cession_pct")[-1]["id"]
    assert client.delete(f"/contracts/{cid}/basis/{entry}", headers=w["ch"]).status_code == 200
    _change(w, cid, "cession_pct", 35, "2027-06-01", approve=False)
    r2, _ = _send_basis(w, cid)
    rd2 = r2.json()["approval"]["round"]
    assert rd2["id"] != rid and rd2["approved"] == 0 and rd2["status"] == "pending"
    assert [c["value"] for c in rd2["changes"]] == ["35%"]


def test_b3_removing_a_change_withdraws_the_request_and_nothing_to_send_is_refused(w):
    cid = _treaty(w, _qs(w, 40), "B3 withdraw", setup=False)
    assert client.post(f"/contracts/{cid}/basis/send-for-approval",
                       headers=w["ch"]).status_code == 409          # nothing entered
    _change(w, cid, "cession_pct", 30, "2027-06-01", approve=False)
    _change(w, cid, "ceding_commission_pct", 25, "2027-06-01", approve=False)
    r, sessions = _send_basis(w, cid)
    rid = r.json()["approval"]["round"]["id"]
    entry = _versions(w, cid, "cession_pct")[-1]["id"]
    assert client.delete(f"/contracts/{cid}/basis/{entry}", headers=w["ch"]).status_code == 200
    b = client.get(f"/contracts/{cid}/basis", headers=w["ch"]).json()
    # The request is withdrawn; the other change is a draft again.
    assert b["approval"]["round"] is None and b["approval"]["draft"] == 1
    token, h = sessions[0]
    assert client.post(f"/treaty-view/{token}/basis/{rid}/approve", headers=h,
                       json={"signer_name": "X"}).status_code == 409
    # Only the carrier sends.
    assert client.post(f"/contracts/{cid}/basis/send-for-approval").status_code == 401
