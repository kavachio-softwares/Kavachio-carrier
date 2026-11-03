"""
Asking for changes — the broker's half of the negotiation.

WHAT THIS FILE IS ABOUT. One rule: what counts as having said something. A
change request that says nothing is a wall rather than a negotiation and is
refused; a request that says it in EITHER form — prose, or a named term
carrying the value wanted — is accepted.

Requiring prose on top of a named term is what these tests exist to stop
coming back. A broker who had filled the row exactly (the term, what it says
now, what they want, why) was left staring at a button that would not go, with
nothing on screen saying what was missing — the most precise form the request
can take was the one form the API would not take.

The screen keeps the same rule (ContractRecord.requestSaysSomething) so it
cannot drift into offering a button the API refuses.
"""
import os
os.environ["MAIL_ALLOWED_RECIPIENTS"] = "nobody@example.invalid"
os.environ.setdefault("APP_BASE_URL", "http://localhost:5173")
import pytest
from fastapi.testclient import TestClient
import main
from auth_tokens import mint_access_token
from db import AppUser, Contract, Party, Program, ProgramBroker, SessionLocal, Tenant

client = TestClient(main.app)


@pytest.fixture(scope="module")
def w():
    sfx = os.urandom(4).hex()
    with SessionLocal() as s:
        car = Tenant(tenant_name=f"neg-{sfx}", legal_name="Insurisk Specialty")
        s.add(car); s.commit()
        br = Party(tenant_id=car.id, party_type="broker", legal_name="CRC",
                   reference=f"neg-b-{sfx}")
        s.add(br); s.commit()
        pr = Program(tenant_id=car.id, name="Spectrum", is_app_managed=True)
        s.add(pr); s.commit()
        s.add(ProgramBroker(tenant_id=car.id, program_id=pr.id,
                            broker_party_id=br.id, status="active"))
        f = (s.query(AppUser).filter(AppUser.role == "kavachio_admin")
             .order_by(AppUser.id).first())
        dana = AppUser(tenant_id=car.id, email=f"d-{sfx}@x.test", full_name="Dana",
                       role="carrier_admin", invited_by_user_id=f.id)
        s.add(dana); s.commit()
        marco = AppUser(tenant_id=None, broker_party_id=br.id,
                        email=f"m-{sfx}@y.test", full_name="Marco",
                        role="broker_admin", invited_by_user_id=dana.id)
        s.add(marco); s.commit()
        return {"t": car.id, "b": br.id, "p": pr.id,
                "ch": {"Authorization":
                       f"Bearer {mint_access_token(dana.id, car.id, 'carrier_admin')}"},
                "bh": {"Authorization":
                       f"Bearer {mint_access_token(marco.id, None, 'broker_admin')}"}}


def _contract(w):
    with SessionLocal() as s:
        c = Contract(tenant_id=w["t"], program_id=w["p"], broker_party_id=w["b"],
                     status="drafted", lifecycle="in_review",
                     commercial_terms={"commission_pct": {"value": "25"}},
                     wording_sections={"sections": [
                         {"title": "Cover", "body": "The Broker may bind."}]})
        s.add(c); s.commit()
        return c.id


def test_a_named_term_alone_is_enough(w):
    """The bug: a broker who filled the row exactly could not send it."""
    cid = _contract(w)
    r = client.post(f"/contracts/{cid}/request-changes", headers=w["bh"], json={
        "note": "",
        "changes": [{"field": "commission_pct", "current": "25",
                     "proposed": "26", "comment": "its mandatory"}]})
    assert r.status_code == 200, r.text
    got = r.json()["open_change_request"]["proposed_changes"]
    assert got[0]["proposed"] == "26"


def test_prose_alone_is_still_enough(w):
    cid = _contract(w)
    r = client.post(f"/contracts/{cid}/request-changes", headers=w["bh"],
                    json={"note": "Commission must be 26%", "changes": []})
    assert r.status_code == 200, r.text


def test_saying_nothing_at_all_is_still_refused(w):
    """A wall is not a negotiation — that rule has not moved."""
    cid = _contract(w)
    r = client.post(f"/contracts/{cid}/request-changes", headers=w["bh"],
                    json={"note": "", "changes": []})
    assert r.status_code == 400, r.text
    assert "naming a term" in r.json()["detail"]["message"]


def test_a_named_term_with_no_value_is_not_saying_anything(w):
    """Clicking 'Name a term' and typing nothing must not count."""
    cid = _contract(w)
    r = client.post(f"/contracts/{cid}/request-changes", headers=w["bh"], json={
        "note": "  ",
        "changes": [{"field": "commission_pct", "current": "25",
                     "proposed": "", "comment": ""}]})
    assert r.status_code == 400, r.text


def test_the_commission_is_a_term_the_request_may_name(w):
    """The agreed limits are in the server's vocabulary — it is only the
    screen's dropdown that does not offer them yet."""
    cid = _contract(w)
    r = client.post(f"/contracts/{cid}/request-changes", headers=w["bh"], json={
        "note": "", "changes": [{"field": "commission_max_pct",
                                 "proposed": "26"}]})
    assert r.status_code == 200, r.text
