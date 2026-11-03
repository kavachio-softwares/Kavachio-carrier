"""
Raising the OTHER kind of contract — insurer ↔ reinsurer.

WHAT THIS FILE IS ABOUT. There have always been two contract types, and the
server has always known the difference: a binder needs the class of business
the broker may write under; a treaty needs the year of account it attaches to
and the notice needed to get out of it. Only one of the two could actually be
raised, because the create screen had no way to say which you were making and
sent a hand-picked six fields that did not include a year of account.

So these tests hold the treaty path open end to end: the spec the form is built
from, the create that must succeed with a treaty's own terms, and the three
refusals that make the type mean something — a missing year of account, a
broker on the wrong side of a treaty, and a reinsurer that is (correctly) not
on the programme being accepted anyway.
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
    """A carrier with both kinds of counterparty: a broker ON its programme,
    and a reinsurer that is deliberately NOT — which is the normal state of a
    reinsurer and the thing the treaty path must not trip over."""
    sfx = os.urandom(4).hex()
    with SessionLocal() as s:
        car = Tenant(tenant_name=f"rein-{sfx}", legal_name="Insurisk Specialty")
        s.add(car); s.commit()
        # One at a time: party_type is a Postgres enum and the ORM maps it as a
        # string, so a two-row insert goes down SQLAlchemy's insertmanyvalues
        # path and casts the parameter to varchar, which the enum column
        # refuses. Every other test in this suite adds parties singly too.
        br = Party(tenant_id=car.id, party_type="broker", legal_name="CRC",
                   reference=f"rein-b-{sfx}", is_active=True)
        s.add(br); s.commit()
        re_ = Party(tenant_id=car.id, party_type="reinsurer",
                    legal_name="Swiss Re", reference=f"rein-r-{sfx}",
                    is_active=True)
        s.add(re_); s.commit()
        pr = Program(tenant_id=car.id, name="Spectrum", is_app_managed=True)
        s.add(pr); s.commit()
        # The broker is on the programme. The reinsurer is not, and never will
        # be — it produces no business into it.
        s.add(ProgramBroker(tenant_id=car.id, program_id=pr.id,
                            broker_party_id=br.id, status="active"))
        f = (s.query(AppUser).filter(AppUser.role == "kavachio_admin")
             .order_by(AppUser.id).first())
        dana = AppUser(tenant_id=car.id, email=f"d-{sfx}@x.test", full_name="Dana",
                       role="carrier_admin", invited_by_user_id=f.id)
        s.add(dana); s.commit()
        return {"t": car.id, "b": br.id, "r": re_.id, "p": pr.id,
                "ch": {"Authorization":
                       f"Bearer {mint_access_token(dana.id, car.id, 'carrier_admin')}"}}


def _treaty(w, **over):
    body = {"program_id": w["p"], "contract_type": "insurer_reinsurer",
            "counterparty_party_id": w["r"], "name": "Property Cat XL 2027",
            "year_of_account": "2027", "notice_period_days": 90,
            "create_as": "draft", **TERM}
    body.update(over)
    for k in [k for k, v in body.items() if v is None]:
        body.pop(k)
    return client.post("/contracts", headers=w["ch"], json=body)


# ── the spec the form is built from ────────────────────────────────────────
def test_the_spec_asks_each_type_for_its_own_terms(w):
    """The form renders whatever this says, so what it says IS the flow."""
    r = client.get("/contract-types", headers=w["ch"])
    assert r.status_code == 200, r.text
    types = {t["key"]: t for t in r.json()["types"]}
    assert set(types) == {"insurer_broker", "insurer_reinsurer"}

    req = {k: {f["name"] for f in t["fields"] if f["required"]}
           for k, t in types.items()}
    # The four that differ. Everything else is common to both.
    assert "class_of_business" in req["insurer_broker"]
    assert "class_of_business" not in req["insurer_reinsurer"]
    assert {"year_of_account", "notice_period_days"} <= req["insurer_reinsurer"]
    assert not {"year_of_account", "notice_period_days"} & req["insurer_broker"]

    assert types["insurer_reinsurer"]["counterparty_party_type"] == "reinsurer"
    assert types["insurer_reinsurer"]["counterparty_label"] == "Reinsurer"
    assert types["insurer_reinsurer"]["counterparty_must_be_on_programme"] is False
    assert types["insurer_broker"]["counterparty_must_be_on_programme"] is True


def test_the_reinsurer_list_is_not_narrowed_by_the_programme(w):
    """The dropdown the screen fills. A reinsurer on no programme still has to
    appear, or the type could be chosen and never completed."""
    r = client.get("/counterparties", headers=w["ch"],
                   params={"party_type": "reinsurer", "program_id": w["p"]})
    assert r.status_code == 200, r.text
    assert w["r"] in [c["id"] for c in r.json()]

    # And the broker gate still bites on the other type.
    r = client.get("/counterparties", headers=w["ch"],
                   params={"party_type": "broker", "program_id": w["p"]})
    assert [c["id"] for c in r.json()] == [w["b"]]


# ── raising one ─────────────────────────────────────────────────────────────
def test_a_treaty_is_created_with_the_terms_its_type_asks_for(w):
    r = _treaty(w)
    assert r.status_code == 200, r.text
    with SessionLocal() as s:
        c = s.get(Contract, r.json()["id"])
        assert c.contract_type == "insurer_reinsurer"
        assert c.broker_party_id == w["r"]
        # The two fields the old create screen never sent.
        assert c.year_of_account == "2027"
        assert c.notice_period_days == 90


def test_a_reinsurer_needs_no_place_on_the_programme(w):
    """Same call as above, stated as its own rule: nothing links the reinsurer
    to the programme, and that must not be an error."""
    with SessionLocal() as s:
        assert s.query(ProgramBroker).filter(
            ProgramBroker.program_id == w["p"],
            ProgramBroker.broker_party_id == w["r"]).count() == 0
    assert _treaty(w).status_code == 200


def test_a_treaty_without_its_year_of_account_is_refused(w):
    """Exactly what the old screen produced: everything else filled in, and no
    year of account in the request at all."""
    r = _treaty(w, year_of_account=None)
    assert r.status_code == 400, r.text
    assert "year_of_account" in r.json()["detail"]["errors"]


def test_a_treaty_without_its_notice_period_is_refused(w):
    r = _treaty(w, notice_period_days=None)
    assert r.status_code == 400, r.text
    assert "notice_period_days" in r.json()["detail"]["errors"]


def test_a_treaty_cannot_be_written_with_a_broker(w):
    """The counterparty type is enforced, not decorative — a contract filed
    against the wrong kind of party sits under a page that cannot show it."""
    r = _treaty(w, counterparty_party_id=w["b"])
    assert r.status_code == 400, r.text
    assert "counterparty_party_id" in r.json()["detail"]["errors"]


def test_a_treaty_needs_no_class_of_business(w):
    """Nobody writes business under a treaty, so the field the binder cannot do
    without is not asked for here."""
    assert _treaty(w, class_of_business=None).status_code == 200


# ── the binder is unchanged ─────────────────────────────────────────────────
def test_a_binder_still_needs_its_class_of_business(w):
    r = client.post("/contracts", headers=w["ch"], json={
        "program_id": w["p"], "contract_type": "insurer_broker",
        "counterparty_party_id": w["b"], "name": "Schedule A — 2027",
        "create_as": "draft", **TERM})
    assert r.status_code == 400, r.text
    assert "class_of_business" in r.json()["detail"]["errors"]


def test_a_binder_still_cannot_be_written_with_a_reinsurer(w):
    r = client.post("/contracts", headers=w["ch"], json={
        "program_id": w["p"], "contract_type": "insurer_broker",
        "counterparty_party_id": w["r"], "name": "Schedule A — 2027",
        "class_of_business": "Commercial Property", "create_as": "draft", **TERM})
    assert r.status_code == 400, r.text
    assert "counterparty_party_id" in r.json()["detail"]["errors"]
