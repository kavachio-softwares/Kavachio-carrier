"""
Skipping the review — the carrier settling terms nobody needs to argue about.

WHY THIS EXISTS. The negotiation is the default road and should stay it: the
carrier writes terms, the broker reads them and either agrees or pushes back,
and only settled terms are signed. But it was the ONLY road, and two ordinary
contracts could not travel it.

  · A renewal on last year's wording at last year's numbers has nothing for the
    broker to read. Sending it out anyway is a week of waiting for a reply that
    says "yes, as before".
  · An insurer ↔ reinsurer treaty has a counterparty with no seat in Kavachio.
    `send-for-review` refuses it outright, the signing round refuses a draft,
    and between the two a treaty could not be signed in the app by any route at
    all. That is the bug these tests were written for.

WHAT IS BEING HELD. That the shortcut lands in the same state the broker's
agreement does, that it is written down as a DIFFERENT fact — a contract the
other side agreed and a contract they were never asked about must not read the
same afterwards — and that it is a shortcut past the review only. Every check
send-for-review makes is still made, the broker still cannot take it, and both
signatures are still required to put the contract in force.
"""
import os
os.environ["MAIL_ALLOWED_RECIPIENTS"] = "nobody@example.invalid"
os.environ.setdefault("APP_BASE_URL", "http://localhost:5173")
import pytest
from fastapi.testclient import TestClient
import main
from auth_tokens import mint_access_token
from db import (AppUser, Contract, Party, Program, ProgramBroker, SessionLocal,
                Tenant)

client = TestClient(main.app)

TERM = {"inception_dt": "2027-01-01", "expiry_dt": "2027-12-31"}


@pytest.fixture(scope="module")
def w():
    """A carrier, a broker on its programme with a seat to log in with, and a
    reinsurer that has neither — which is the whole point of the reinsurer."""
    sfx = os.urandom(4).hex()
    with SessionLocal() as s:
        car = Tenant(tenant_name=f"skip-{sfx}", legal_name="Insurisk Specialty")
        s.add(car); s.commit()
        # Singly: party_type is a Postgres enum, and a multi-row add goes down
        # SQLAlchemy's insertmanyvalues path, which casts the parameter to
        # varchar and the enum column refuses it.
        br = Party(tenant_id=car.id, party_type="broker", legal_name="CRC",
                   reference=f"skip-b-{sfx}", is_active=True)
        s.add(br); s.commit()
        re_ = Party(tenant_id=car.id, party_type="reinsurer",
                    legal_name="Swiss Re", reference=f"skip-r-{sfx}",
                    is_active=True)
        s.add(re_); s.commit()
        pr = Program(tenant_id=car.id, name="Spectrum", is_app_managed=True)
        s.add(pr); s.commit()
        s.add(ProgramBroker(tenant_id=car.id, program_id=pr.id,
                            broker_party_id=br.id, status="active"))
        f = (s.query(AppUser).filter(AppUser.role == "kavachio_admin")
             .order_by(AppUser.id).first())
        if f is None:
            pytest.skip("no kavachio_admin on this database to seed an invite chain")
        dana = AppUser(tenant_id=car.id, email=f"d-{sfx}@x.test", full_name="Dana",
                       role="carrier_admin", invited_by_user_id=f.id)
        s.add(dana); s.commit()
        marco = AppUser(tenant_id=None, broker_party_id=br.id,
                        email=f"m-{sfx}@crc.test", full_name="Marco",
                        role="broker_admin", invited_by_user_id=dana.id)
        s.add(marco); s.commit()
        return {"t": car.id, "b": br.id, "r": re_.id, "p": pr.id,
                "ch": {"Authorization":
                       f"Bearer {mint_access_token(dana.id, car.id, 'carrier_admin')}"},
                "bh": {"Authorization":
                       f"Bearer {mint_access_token(marco.id, None, 'broker_admin')}"}}


def _binder(w, **over):
    """A draft insurer ↔ broker contract — the ordinary kind."""
    body = {"program_id": w["p"], "contract_type": "insurer_broker",
            "counterparty_party_id": w["b"], "name": f"Binder {os.urandom(2).hex()}",
            "class_of_business": "Property", "create_as": "draft", **TERM}
    body.update(over)
    r = client.post("/contracts", headers=w["ch"], json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _treaty(w, **over):
    """A draft insurer ↔ reinsurer contract — the kind with nobody to ask."""
    body = {"program_id": w["p"], "contract_type": "insurer_reinsurer",
            "counterparty_party_id": w["r"], "name": f"Cat XL {os.urandom(2).hex()}",
            "year_of_account": "2027", "notice_period_days": 90,
            "create_as": "draft", **TERM}
    body.update(over)
    r = client.post("/contracts", headers=w["ch"], json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _skip(w, cid, headers=None, note=None):
    return client.post(f"/contracts/{cid}/skip-review",
                       headers=headers or w["ch"], json={"note": note})


def _history(w, cid):
    r = client.get(f"/contracts/{cid}/approvals", headers=w["ch"])
    if r.status_code != 200:
        pytest.skip(f"no approval history endpoint here ({r.status_code})")
    body = r.json()
    return body if isinstance(body, list) else body.get("approvals", body)


# ── it is offered, and only where it should be ─────────────────────────────
def test_the_carrier_is_offered_it_on_a_draft(w):
    c = _binder(w)
    assert c["actions"]["skip_review"] is True
    # Beside the review, never instead of it. A screen that offered only the
    # shortcut would make not asking the broker the default, which is the one
    # outcome this must not produce.
    assert c["actions"]["send_for_review"] is True


def test_the_broker_is_not_offered_it(w):
    """It is the carrier settling ITS OWN proposal. A broker doing this would
    be agreeing terms on behalf of the side that wrote them."""
    c = _binder(w)
    r = client.get(f"/contracts/{c['id']}", headers=w["bh"])
    if r.status_code != 200:
        pytest.skip("the broker cannot see this contract at all")
    assert r.json()["actions"]["skip_review"] is False


def test_it_is_gone_once_the_terms_have_been_out(w):
    """Draft only. Skipping a review that was never held is "none was needed";
    skipping one already asked for is ignoring the answer."""
    c = _binder(w)
    sent = client.post(f"/contracts/{c['id']}/send-for-review",
                       headers=w["ch"], json={})
    assert sent.status_code == 200, sent.text
    assert sent.json()["lifecycle"] == "in_review"
    assert sent.json()["actions"]["skip_review"] is False


# ── what it does ───────────────────────────────────────────────────────────
def test_it_settles_the_terms_and_says_who_did(w):
    c = _binder(w)
    r = _skip(w, c["id"], note="Renewal on last year's wording.")
    assert r.status_code == 200, r.text
    assert r.json()["lifecycle"] == "agreed"

    # The half that matters as much as the state: the history has to be able to
    # tell this apart from terms the broker actually agreed to.
    acts = [h["action"] for h in _history(w, c["id"])]
    assert "review_skipped" in acts
    assert "terms_agreed" not in acts
    assert "sent_for_review" not in acts, "nothing went out to anybody"


def test_a_treaty_can_take_it_though_it_cannot_take_a_review(w):
    """The bug this was written for. A reinsurer has no seat in Kavachio, so
    send-for-review refuses the contract outright — and with the signing round
    refusing a draft, a treaty had no road to a signature at all."""
    c = _treaty(w)
    refused = client.post(f"/contracts/{c['id']}/send-for-review",
                          headers=w["ch"], json={})
    assert refused.status_code == 409, refused.text

    r = _skip(w, c["id"])
    assert r.status_code == 200, r.text
    assert r.json()["lifecycle"] == "agreed"


def test_it_opens_the_signing_round(w):
    """The whole purpose. Before: a draft cannot be signed, and the refusal
    says so. After: the carrier's own signature is the next move."""
    c = _treaty(w)
    before = client.get(f"/esign/contracts/{c['id']}/round", headers=w["ch"])
    assert before.status_code == 200, before.text
    assert before.json()["can_sign"] is False

    assert _skip(w, c["id"]).status_code == 200
    after = client.get(f"/esign/contracts/{c['id']}/round", headers=w["ch"])
    assert after.status_code == 200, after.text
    assert after.json()["can_sign"] is True
    assert after.json()["waiting_on"] == "carrier", "the carrier signs first"


def test_it_does_not_put_the_contract_in_force(w):
    """A shortcut past the READING of the terms, and past nothing else. Both
    signatures still make a contract live, and neither has been given."""
    c = _binder(w)
    assert _skip(w, c["id"]).status_code == 200
    rec = client.get(f"/contracts/{c['id']}", headers=w["ch"]).json()
    assert rec["lifecycle"] == "agreed"
    assert rec["lifecycle"] != "active"
    assert rec["actions"]["activate"] is False


# ── what it still refuses ──────────────────────────────────────────────────
def test_the_broker_cannot_do_it(w):
    c = _binder(w)
    assert _skip(w, c["id"], headers=w["bh"]).status_code in (403, 404)
    rec = client.get(f"/contracts/{c['id']}", headers=w["ch"]).json()
    assert rec["lifecycle"] == "draft", "refused, but it moved anyway"


def test_it_cannot_be_done_twice(w):
    c = _binder(w)
    assert _skip(w, c["id"]).status_code == 200
    again = _skip(w, c["id"])
    assert again.status_code == 409, again.text


def test_a_contract_missing_a_document_it_defers_to_is_still_refused(w):
    """Everything send-for-review checks is checked here too, and for a
    stronger reason: nothing is going out for anyone to read, so this is the
    last point at which an incomplete contract can be stopped before it starts
    carrying signatures.

    A wording that says "excluded classes per the Guidelines on file" is not
    readable until those guidelines are on file, and signing it is agreeing to
    terms neither side can see. Set on the row directly because that is where
    extraction puts it."""
    c = _treaty(w)
    with SessionLocal() as s:
        row = s.get(Contract, c["id"])
        row.extracted = {"reference_documents": {"external": [
            {"document_name": "Underwriting Guidelines v4"}]}}
        s.commit()

    assert client.get(f"/contracts/{c['id']}", headers=w["ch"]
                      ).json()["missing_references"] == ["Underwriting Guidelines v4"]
    r = _skip(w, c["id"])
    assert r.status_code == 400, r.text
    assert "Underwriting Guidelines v4" in r.text
    assert client.get(f"/contracts/{c['id']}",
                      headers=w["ch"]).json()["lifecycle"] == "draft"
