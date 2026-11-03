"""The carrier admin's say on WHO the carrier works with.

THE GAP. A carrier USER could bring a broker on board outright. The broker
organisation and its admin login were created and an invitation was mailed the
instant they clicked, so by the time the carrier admin could have had an opinion
there was nothing left to decide.

What must now hold:
  · a carrier user's add/invite creates a REQUEST and NOTHING else — no party,
    no login, no relationship, no programme link, no invitation, no mail
  · the carrier ADMIN's own act is unchanged: link live, invitation sent
  · approving runs the ordinary onboarding, and writes the programme link at
    `pending_approval` so the BORDEREAU SETUP GATE STILL RELEASES IT
  · rejecting leaves nothing behind, keeps the reason, and can never be
    approved afterwards
  · a carrier user cannot decide, and Kavachio cannot decide at all
  · an invitation that names a programme no longer hands the broker a LIVE
    link on accept when a carrier user raised it

    python -m pytest test_broker_onboarding_gate.py
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

import main  # noqa: F401 — loads .env, which db needs at import
import broker_routes
import hierarchy_routes
from auth_tokens import mint_access_token
from carrier_scope import LINK_PENDING
from db import (
    AppUser, BrokerInvitation, BrokerOnboardingRequest, CarrierBroker, Party,
    Program, ProgramBroker, SessionLocal, Tenant,
)

client = TestClient(main.app)


@pytest.fixture()
def w():
    """One carrier with BOTH seats, one programme, one broker already on board.

    The seats are what this whole suite is about: `owner_user_id` is the only
    thing separating them, so the fixture sets it explicitly rather than letting
    carrier_seat fall back to "both", which would open every gate under test.
    """
    from ingester import _ensure_carrier_party
    sfx = os.urandom(4).hex()
    made: list[tuple[type, int]] = []
    with SessionLocal() as s:
        founder = (s.query(AppUser).filter(AppUser.role == "kavachio_admin")
                   .order_by(AppUser.id).first())
        if founder is None:
            pytest.skip("no kavachio_admin on this database to seed an invite chain")

        def add(row):
            s.add(row); s.commit(); made.append((type(row), row.id))
            return row

        carrier = add(Tenant(tenant_name=f"bog-{sfx}", legal_name="Gate Carrier"))
        cpid = _ensure_carrier_party(s, carrier.id); s.commit()
        made.append((Party, cpid))
        prog = add(Program(tenant_id=carrier.id, name=f"BOG {sfx}",
                           is_app_managed=True))
        # A broker already in the directory — the "select existing" shape.
        broker = add(Party(tenant_id=carrier.id, party_type="broker",
                           legal_name=f"Gate Broker {sfx}", reference=f"bog-b-{sfx}"))
        add(CarrierBroker(tenant_id=carrier.id, party_id=broker.id,
                          status="active", origin="onboarded"))

        admin = add(AppUser(tenant_id=carrier.id, email=f"bog-a-{sfx}@carrier.test",
                            full_name="Carrier Admin", role="carrier_admin",
                            invited_by_user_id=founder.id))
        user = add(AppUser(tenant_id=carrier.id, email=f"bog-u-{sfx}@carrier.test",
                           full_name="Carrier User", role="carrier_admin",
                           invited_by_user_id=founder.id))
        # THE SEAT SPLIT. Without this both are "both" and every gate opens.
        carrier.owner_user_id = admin.id
        s.commit()

        ids = {
            "sfx": sfx, "tid": carrier.id, "prog": prog.id, "broker": broker.id,
            # ORM `tenant_name` IS the `tenant_code` column — the legacy `mga`.
            "code": carrier.tenant_name,
            "admin_id": admin.id, "user_id": user.id,
            "admin_h": {"Authorization":
                        f"Bearer {mint_access_token(admin.id, carrier.id, 'carrier_admin')}"},
            "user_h": {"Authorization":
                       f"Bearer {mint_access_token(user.id, carrier.id, 'carrier_admin')}"},
            "kav_h": {"Authorization":
                      f"Bearer {mint_access_token(founder.id, carrier.id, 'kavachio_admin')}"},
        }
    yield ids
    # By id only, newest first — never a cascade. See the memory note on
    # TRUNCATE party CASCADE taking app_user with it.
    #
    # The requests come FIRST and are found by tenant, not by id: the API made
    # them, not `add()`, so the list below has never heard of them. Without this
    # they pile up run after run and the duplicate guard starts failing tests
    # that have nothing wrong with them.
    with SessionLocal() as s:
        for r in (s.query(BrokerOnboardingRequest)
                  .filter(BrokerOnboardingRequest.tenant_id == ids["tid"]).all()):
            s.delete(r)
        for r in (s.query(ProgramBroker)
                  .filter(ProgramBroker.program_id == ids["prog"]).all()):
            s.delete(r)
        s.commit()
    with SessionLocal() as s:
        for model, pk in reversed(made):
            try:
                obj = s.get(model, pk)
                if obj is not None:
                    s.delete(obj); s.commit()
            except Exception:
                s.rollback()


def _reqs(tid, status=None):
    with SessionLocal() as s:
        q = s.query(BrokerOnboardingRequest).filter(
            BrokerOnboardingRequest.tenant_id == tid)
        if status:
            q = q.filter(BrokerOnboardingRequest.status == status)
        return q.order_by(BrokerOnboardingRequest.id).all()


def _links(prog_id, party_id=None):
    with SessionLocal() as s:
        q = s.query(ProgramBroker).filter(ProgramBroker.program_id == prog_id)
        if party_id:
            q = q.filter(ProgramBroker.broker_party_id == party_id)
        return q.all()


def _cleanup_email(email: str) -> None:
    """Remove whatever an APPROVED invite created, so the fixture's id-only
    teardown is not left with orphans it never saw."""
    with SessionLocal() as s:
        for u in s.query(AppUser).filter(AppUser.email == email).all():
            pid = u.broker_party_id
            s.delete(u); s.commit()
            if pid:
                for t in (ProgramBroker, CarrierBroker):
                    col = (ProgramBroker.broker_party_id if t is ProgramBroker
                           else CarrierBroker.party_id)
                    for r in s.query(t).filter(col == pid).all():
                        s.delete(r)
                s.commit()
                p = s.get(Party, pid)
                if p:
                    s.delete(p); s.commit()
        for i in s.query(BrokerInvitation).filter(
                BrokerInvitation.email == email).all():
            s.delete(i)
        s.commit()


# =============================================================================
#  A carrier USER creates a request and nothing else
# =============================================================================

def test_user_add_existing_broker_makes_a_request_not_a_link(w):
    r = client.post(f"/programs/{w['prog']}/brokers",
                    json={"broker_party_id": w["broker"]}, headers=w["user_h"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pending"] is True
    assert body["link_id"] is None
    assert "your carrier to approve" in body["message"]

    # THE POINT: no link at all, not even a pending one.
    assert _links(w["prog"]) == []
    rows = _reqs(w["tid"])
    assert len(rows) == 1
    assert (rows[0].status, rows[0].broker_party_id, rows[0].program_id) \
        == ("pending", w["broker"], w["prog"])
    assert rows[0].requested_by_user_id == w["user_id"]


def test_user_invite_creates_no_party_no_login_no_invitation(w):
    email = f"bog-new-{w['sfx']}@broker.test"
    r = client.post("/brokers", headers=w["user_h"], json={
        "legal_name": f"Gate New {w['sfx']}", "party_type": "broker",
        "admin_name": "Nobody Yet", "admin_email": email})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pending"] is True
    # The old response said "Invitation sent to …". It must not any more.
    assert body["invited"] is False
    assert "Invitation sent" not in body["message"]

    with SessionLocal() as s:
        assert s.query(AppUser).filter(AppUser.email == email).first() is None
        assert s.query(BrokerInvitation).filter(
            BrokerInvitation.email == email).first() is None
        assert s.query(Party).filter(
            Party.legal_name == f"Gate New {w['sfx']}").first() is None
    rows = _reqs(w["tid"])
    assert len(rows) == 1
    assert (rows[0].status, rows[0].broker_party_id, rows[0].email) \
        == ("pending", None, email)


def test_second_ask_about_the_same_broker_is_refused(w):
    client.post(f"/programs/{w['prog']}/brokers",
                json={"broker_party_id": w["broker"]}, headers=w["user_h"])
    again = client.post(f"/programs/{w['prog']}/brokers",
                        json={"broker_party_id": w["broker"]}, headers=w["user_h"])
    assert again.status_code == 409
    assert len(_reqs(w["tid"], "pending")) == 1


def test_a_duplicate_org_name_is_refused_at_the_moment_it_is_typed(w):
    """The refusals that used to reach the carrier user instantly still do.

    This is the one that matters most: before, they heard about it the moment
    they typed it. If it only surfaced at approval, their carrier admin would be
    the one reading it and would have to relay it back."""
    with SessionLocal() as s:
        # A party of this carrier's already holds the name.
        name = s.get(Party, w["broker"]).legal_name
    r = client.post("/brokers", headers=w["user_h"], json={
        "legal_name": name, "party_type": "broker",
        "admin_name": "X", "admin_email": f"bog-dup-{w['sfx']}@broker.test"})
    assert r.status_code == 409, r.text
    assert "already work with a broker called" in r.text
    assert _reqs(w["tid"]) == []


def test_an_address_held_by_a_non_broker_reveals_nothing_either_way(w):
    """A carrier's own staff address. The ORIGINAL deliberately does not refuse
    this — it records an invitation that can never be accepted and answers
    exactly as a real one does, so the carrier cannot discover by typing an
    address whose it is.

    That property has to survive the extra step. So a request IS raised, it
    looks like any other, and approving it creates the same dead invitation and
    mails NOBODY. The one thing that must not happen is the staff member being
    written to."""
    with SessionLocal() as s:
        taken = (s.query(AppUser).filter(AppUser.role == "kavachio_admin")
                 .order_by(AppUser.id).first()).email
        before_users = s.query(AppUser).filter(AppUser.email == taken).count()
    r = client.post("/brokers", headers=w["user_h"], json={
        "legal_name": f"Gate Dead {w['sfx']}", "party_type": "broker",
        "admin_name": "X", "admin_email": taken})
    assert r.status_code == 200, r.text
    assert r.json()["pending"] is True
    rid = _reqs(w["tid"])[0].id

    inv_id = None
    try:
        assert client.post(f"/broker-onboarding-requests/{rid}/approve",
                           headers=w["admin_h"]).status_code == 200
        with SessionLocal() as s:
            # No second login, no party, no programme link for them.
            assert s.query(AppUser).filter(
                AppUser.email == taken).count() == before_users
            assert s.query(Party).filter(
                Party.legal_name == f"Gate Dead {w['sfx']}").first() is None
            assert _links(w["prog"]) == []
            inv = s.query(BrokerInvitation).filter(
                BrokerInvitation.tenant_id == w["tid"],
                BrokerInvitation.email == taken).first()
            # The dead invitation, exactly as the admin's own path writes it.
            assert inv is not None and inv.party_id is None
            inv_id = inv.id
    finally:
        if inv_id:
            with SessionLocal() as s:
                row = s.get(BrokerInvitation, inv_id)
                if row:
                    s.delete(row); s.commit()


# =============================================================================
#  The carrier ADMIN's own act is untouched
# =============================================================================

def test_admin_add_links_immediately_and_raises_no_request(w):
    r = client.post(f"/programs/{w['prog']}/brokers",
                    json={"broker_party_id": w["broker"]}, headers=w["admin_h"])
    assert r.status_code == 200, r.text
    assert r.json().get("pending") is not True
    links = _links(w["prog"], w["broker"])
    assert len(links) == 1 and links[0].status == "active"
    assert _reqs(w["tid"]) == []


# =============================================================================
#  Deciding
# =============================================================================

def test_approve_links_at_pending_so_the_setup_gate_still_releases_it(w):
    client.post(f"/programs/{w['prog']}/brokers",
                json={"broker_party_id": w["broker"]}, headers=w["user_h"])
    rid = _reqs(w["tid"])[0].id

    r = client.post(f"/broker-onboarding-requests/{rid}/approve",
                    headers=w["admin_h"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"

    links = _links(w["prog"], w["broker"])
    assert len(links) == 1
    # NOT "active". This gate settles who we work with; the Bordereau Setup
    # gate still decides what they may send, and it reads exactly this status.
    assert links[0].status == LINK_PENDING
    # Recorded under the person who asked, not the admin who agreed.
    assert links[0].assigned_by_user_id == w["user_id"]


def test_approve_invite_onboards_and_links_pending(w):
    email = f"bog-appr-{w['sfx']}@broker.test"
    org = f"Gate Appr {w['sfx']}"
    try:
        client.post("/brokers", headers=w["user_h"], json={
            "legal_name": org, "party_type": "broker",
            "admin_name": "Nobody Yet", "admin_email": email})
        rid = _reqs(w["tid"])[0].id
        # The programme was not named on this one (the Party-screen shape), so
        # set it as the programme add would have, to prove the link is written.
        with SessionLocal() as s:
            s.get(BrokerOnboardingRequest, rid).program_id = w["prog"]
            s.commit()

        r = client.post(f"/broker-onboarding-requests/{rid}/approve",
                        headers=w["admin_h"])
        assert r.status_code == 200, r.text

        with SessionLocal() as s:
            u = s.query(AppUser).filter(AppUser.email == email).first()
            assert u is not None and u.role == "broker_admin"
            assert u.status == "invited"          # a token was stamped
            party = s.get(Party, u.broker_party_id)
            assert party is not None and party.legal_name == org
            inv = s.query(BrokerInvitation).filter(
                BrokerInvitation.email == email).first()
            assert inv is not None and inv.status == "pending"
            link = (s.query(ProgramBroker)
                    .filter(ProgramBroker.program_id == w["prog"],
                            ProgramBroker.broker_party_id == party.id).first())
            assert link is not None and link.status == LINK_PENDING
    finally:
        _cleanup_email(email)


def test_reject_needs_a_reason_keeps_it_and_creates_nothing(w):
    email = f"bog-rej-{w['sfx']}@broker.test"
    client.post("/brokers", headers=w["user_h"], json={
        "legal_name": f"Gate Rej {w['sfx']}", "party_type": "broker",
        "admin_name": "Nobody Yet", "admin_email": email})
    rid = _reqs(w["tid"])[0].id

    bare = client.post(f"/broker-onboarding-requests/{rid}/reject",
                       json={}, headers=w["admin_h"])
    assert bare.status_code == 400

    r = client.post(f"/broker-onboarding-requests/{rid}/reject",
                    json={"reason": "We already produce this class."},
                    headers=w["admin_h"])
    assert r.status_code == 200, r.text
    assert r.json()["reason"] == "We already produce this class."

    with SessionLocal() as s:
        row = s.get(BrokerOnboardingRequest, rid)
        assert row.status == "rejected"
        assert row.decided_by_user_id == w["admin_id"]
        # NOTHING was created, so nothing had to be undone.
        assert s.query(AppUser).filter(AppUser.email == email).first() is None
        assert s.query(BrokerInvitation).filter(
            BrokerInvitation.email == email).first() is None


def test_a_rejected_request_can_never_be_approved(w):
    client.post(f"/programs/{w['prog']}/brokers",
                json={"broker_party_id": w["broker"]}, headers=w["user_h"])
    rid = _reqs(w["tid"])[0].id
    client.post(f"/broker-onboarding-requests/{rid}/reject",
                json={"reason": "no"}, headers=w["admin_h"])

    again = client.post(f"/broker-onboarding-requests/{rid}/approve",
                        headers=w["admin_h"])
    assert again.status_code == 409
    assert _links(w["prog"]) == []


def test_approving_twice_is_refused(w):
    client.post(f"/programs/{w['prog']}/brokers",
                json={"broker_party_id": w["broker"]}, headers=w["user_h"])
    rid = _reqs(w["tid"])[0].id
    assert client.post(f"/broker-onboarding-requests/{rid}/approve",
                       headers=w["admin_h"]).status_code == 200
    assert client.post(f"/broker-onboarding-requests/{rid}/approve",
                       headers=w["admin_h"]).status_code == 409


# =============================================================================
#  Who may decide
# =============================================================================

def test_carrier_user_cannot_decide_their_own_request(w):
    client.post(f"/programs/{w['prog']}/brokers",
                json={"broker_party_id": w["broker"]}, headers=w["user_h"])
    rid = _reqs(w["tid"])[0].id
    for path, body in ((f"/broker-onboarding-requests/{rid}/approve", None),
                       (f"/broker-onboarding-requests/{rid}/reject",
                        {"reason": "mine"})):
        r = client.post(path, json=body, headers=w["user_h"])
        assert r.status_code == 403, (path, r.status_code, r.text)
    assert _links(w["prog"]) == []


def test_kavachio_cannot_decide_a_carriers_relationship(w):
    client.post(f"/programs/{w['prog']}/brokers",
                json={"broker_party_id": w["broker"]}, headers=w["user_h"])
    rid = _reqs(w["tid"])[0].id
    r = client.post(f"/broker-onboarding-requests/{rid}/approve",
                    headers=w["kav_h"])
    assert r.status_code == 403, r.text
    assert "invite" in r.text.lower() or "carrier" in r.text.lower()


def test_the_queue_is_scoped_to_the_seat(w):
    client.post(f"/programs/{w['prog']}/brokers",
                json={"broker_party_id": w["broker"]}, headers=w["user_h"])
    admin_sees = client.get("/broker-onboarding-requests", headers=w["admin_h"])
    user_sees = client.get("/broker-onboarding-requests", headers=w["user_h"])
    assert admin_sees.status_code == 200 and user_sees.status_code == 200
    assert admin_sees.json()["pending"] == 1
    # Their own, so they can read the reason when it comes back.
    assert user_sees.json()["pending"] == 1
    row = user_sees.json()["items"][0]
    assert row["kind"] == "existing" and row["admin_email"] is None


def test_only_the_asker_withdraws(w):
    client.post(f"/programs/{w['prog']}/brokers",
                json={"broker_party_id": w["broker"]}, headers=w["user_h"])
    rid = _reqs(w["tid"])[0].id
    assert client.delete(f"/broker-onboarding-requests/{rid}",
                         headers=w["admin_h"]).status_code == 403
    assert client.delete(f"/broker-onboarding-requests/{rid}",
                         headers=w["user_h"]).status_code == 200
    with SessionLocal() as s:
        assert s.get(BrokerOnboardingRequest, rid).status == "withdrawn"
    # …and the duplicate guard is released, so they can ask properly.
    assert client.post(f"/programs/{w['prog']}/brokers",
                       json={"broker_party_id": w["broker"]},
                       headers=w["user_h"]).status_code == 200


# =============================================================================
#  The dashboard count
# =============================================================================

def test_dashboard_counts_for_the_admin_and_not_the_user(w):
    client.post(f"/programs/{w['prog']}/brokers",
                json={"broker_party_id": w["broker"]}, headers=w["user_h"])
    # `mga` is a required query param and is legacy — the tenant is resolved
    # from the token regardless, so what is passed does not matter.
    q = {"mga": w["code"]}
    ra = client.get("/dashboard/stats", params=q, headers=w["admin_h"])
    ru = client.get("/dashboard/stats", params=q, headers=w["user_h"])
    assert ra.status_code == 200, ra.text
    assert ru.status_code == 200, ru.text
    a, u = ra.json(), ru.json()
    assert a["broker_requests_pending"] == 1
    # None, not 0: a carrier user is not the one being asked.
    assert u["broker_requests_pending"] is None


# =============================================================================
#  The hole in _accept_invitation
# =============================================================================

def test_accepting_a_user_raised_invitation_does_not_hand_over_a_live_link(w):
    """An invitation that NAMES a programme used to write status='active' on
    accept, whoever raised it — straight past the Bordereau Setup gate."""
    with SessionLocal() as s:
        inv = BrokerInvitation(
            tenant_id=w["tid"], program_id=w["prog"], email="x@y.test",
            party_id=w["broker"], org_name="X", status="pending",
            by_user_id=w["user_id"])          # a carrier USER raised it
        s.add(inv); s.commit()
        try:
            broker_routes._accept_invitation(s, inv, w["broker"], "broker")
            s.commit()
            link = (s.query(ProgramBroker)
                    .filter(ProgramBroker.program_id == w["prog"],
                            ProgramBroker.broker_party_id == w["broker"]).first())
            assert link is not None and link.status == LINK_PENDING
        finally:
            for r in (s.query(ProgramBroker)
                      .filter(ProgramBroker.program_id == w["prog"]).all()):
                s.delete(r)
            s.delete(s.get(BrokerInvitation, inv.id)); s.commit()


def test_the_admins_invitation_still_goes_live_on_accept(w):
    with SessionLocal() as s:
        inv = BrokerInvitation(
            tenant_id=w["tid"], program_id=w["prog"], email="x@y.test",
            party_id=w["broker"], org_name="X", status="pending",
            by_user_id=w["admin_id"])         # the carrier ADMIN raised it
        s.add(inv); s.commit()
        try:
            broker_routes._accept_invitation(s, inv, w["broker"], "broker")
            s.commit()
            link = (s.query(ProgramBroker)
                    .filter(ProgramBroker.program_id == w["prog"],
                            ProgramBroker.broker_party_id == w["broker"]).first())
            assert link is not None and link.status == "active"
        finally:
            for r in (s.query(ProgramBroker)
                      .filter(ProgramBroker.program_id == w["prog"]).all()):
                s.delete(r)
            s.delete(s.get(BrokerInvitation, inv.id)); s.commit()


# =============================================================================
#  The setup gate is still the thing that releases the programme
# =============================================================================

def test_the_setup_approval_still_releases_an_approved_brokers_link(w):
    """End to end across BOTH gates: broker approved → link waiting →
    _activate_links_for (the setup approval's own helper) puts it live."""
    import direct_routes
    from db import Pipeline
    client.post(f"/programs/{w['prog']}/brokers",
                json={"broker_party_id": w["broker"]}, headers=w["user_h"])
    rid = _reqs(w["tid"])[0].id
    client.post(f"/broker-onboarding-requests/{rid}/approve", headers=w["admin_h"])
    assert _links(w["prog"], w["broker"])[0].status == LINK_PENDING

    with SessionLocal() as s:
        p = Pipeline(tenant_id=w["tid"], name=f"BOG pipe {w['sfx']}",
                     program_id=w["prog"], broker_party_id=w["broker"],
                     status=LINK_PENDING)
        s.add(p); s.commit()
        try:
            assert direct_routes._activate_links_for(s, p) == 1
            s.commit()
            assert _links(w["prog"], w["broker"])[0].status == "active"
        finally:
            s.delete(s.get(Pipeline, p.id)); s.commit()


# =============================================================================
#  The screens can SEE that something is waiting
# =============================================================================

def test_hierarchy_reports_a_waiting_request_without_counting_it(w):
    """The bug this fixes: with no link and nothing else said, every screen
    read the programme as untouched and told the person who had just added a
    broker to add one — and doing so was refused as a duplicate."""
    client.post(f"/programs/{w['prog']}/brokers",
                json={"broker_party_id": w["broker"]}, headers=w["user_h"])

    h = client.get("/hierarchy", headers=w["user_h"])
    assert h.status_code == 200, h.text
    prog = next(p for p in h.json()["programmes"] if p["id"] == w["prog"])

    waiting = prog["brokers_awaiting"]
    assert len(waiting) == 1
    assert waiting[0]["broker_party_id"] == w["broker"]
    assert waiting[0]["legal_name"] == f"Gate Broker {w['sfx']}"
    # NOT a broker on the programme. Nothing can be built on one of these, so
    # counting it would make the contract and setup steps offer work that the
    # server refuses.
    assert prog["brokers"] == []
    assert prog["broker_count"] == 0


def test_the_carrier_admin_sees_the_same_waiting_request(w):
    client.post(f"/programs/{w['prog']}/brokers",
                json={"broker_party_id": w["broker"]}, headers=w["user_h"])
    prog = next(p for p in client.get("/hierarchy", headers=w["admin_h"]).json()
                ["programmes"] if p["id"] == w["prog"])
    assert len(prog["brokers_awaiting"]) == 1


def test_a_decided_request_stops_being_reported_as_waiting(w):
    client.post(f"/programs/{w['prog']}/brokers",
                json={"broker_party_id": w["broker"]}, headers=w["user_h"])
    rid = _reqs(w["tid"])[0].id
    client.post(f"/broker-onboarding-requests/{rid}/approve", headers=w["admin_h"])

    prog = next(p for p in client.get("/hierarchy", headers=w["user_h"]).json()
                ["programmes"] if p["id"] == w["prog"])
    assert prog["brokers_awaiting"] == []
    # …and now it IS a broker on the programme, so the contract step opens.
    assert [b["id"] for b in prog["brokers"]] == [w["broker"]]
    assert prog["brokers"][0]["link_status"] == LINK_PENDING


def test_asking_twice_says_why_in_plain_words(w):
    """The refusal a carrier user actually hits. It has to name the real
    reason: the screen used to replace it with a fixed sentence about accepting
    an invitation, which was not what happened and gave them nothing to do."""
    client.post(f"/programs/{w['prog']}/brokers",
                json={"broker_party_id": w["broker"]}, headers=w["user_h"])
    again = client.post(f"/programs/{w['prog']}/brokers",
                        json={"broker_party_id": w["broker"]}, headers=w["user_h"])
    assert again.status_code == 409
    said = again.json()["detail"]["message"]
    assert "already asked your carrier" in said
    assert "waiting on them" in said


def test_inviting_from_a_programme_carries_the_programme_all_the_way(w):
    """Invite raised from a programme screen. The programme has to survive the
    whole way: onto the request (so the admin's queue names it instead of
    saying "No programme yet"), and into the programme link when approved.

    Before, `inviteBroker` never sent program_id, so the broker and the
    programme were two separate asks and the second could only be made after
    the first was approved."""
    email = f"bog-prog-{w['sfx']}@broker.test"
    org = f"Gate Prog {w['sfx']}"
    try:
        r = client.post("/brokers", headers=w["user_h"], json={
            "legal_name": org, "party_type": "broker",
            "admin_name": "Nobody Yet", "admin_email": email,
            "program_id": w["prog"]})
        assert r.status_code == 200 and r.json()["pending"] is True, r.text

        req = _reqs(w["tid"])[0]
        assert req.program_id == w["prog"]

        # The carrier admin's queue names the programme.
        row = next(i for i in client.get("/broker-onboarding-requests",
                                         headers=w["admin_h"]).json()["items"]
                   if i["id"] == req.id)
        assert row["programme"] == f"BOG {w['sfx']}"
        assert row["kind"] == "invite" and row["admin_email"] == email

        # …and so does the programme's own screen, by the typed org name —
        # there is no Party for it yet.
        prog = next(p for p in client.get("/hierarchy", headers=w["user_h"])
                    .json()["programmes"] if p["id"] == w["prog"])
        assert [b["legal_name"] for b in prog["brokers_awaiting"]] == [org]

        # Approving does BOTH: onboards them and puts them on the programme.
        assert client.post(f"/broker-onboarding-requests/{req.id}/approve",
                           headers=w["admin_h"]).status_code == 200
        with SessionLocal() as s:
            u = s.query(AppUser).filter(AppUser.email == email).first()
            assert u is not None
            link = (s.query(ProgramBroker)
                    .filter(ProgramBroker.program_id == w["prog"],
                            ProgramBroker.broker_party_id == u.broker_party_id)
                    .first())
            assert link is not None and link.status == LINK_PENDING
    finally:
        _cleanup_email(email)


def test_an_invite_with_no_programme_still_says_so(w):
    """The Party screen genuinely has none — a broker can join the directory
    before anybody decides where they produce. That must keep working."""
    email = f"bog-noprog-{w['sfx']}@broker.test"
    r = client.post("/brokers", headers=w["user_h"], json={
        "legal_name": f"Gate NoProg {w['sfx']}", "party_type": "broker",
        "admin_name": "Nobody Yet", "admin_email": email})
    assert r.status_code == 200, r.text
    req = _reqs(w["tid"])[0]
    assert req.program_id is None
    row = next(i for i in client.get("/broker-onboarding-requests",
                                     headers=w["admin_h"]).json()["items"]
               if i["id"] == req.id)
    assert row["programme"] is None


# =============================================================================
#  The Programmes wizard cannot advance to Bordereau Setup on an unsigned
#  contract — /hierarchy's per-contract `settled` flag, and the rule behind it
# =============================================================================

def test_settled_matches_the_real_pipeline_ready_gate(w):
    """hierarchy_routes._contract_settled must agree with
    direct_routes._pipeline_ready exactly — that is the whole point of reading
    it off the server rather than re-deriving lifecycle logic in the browser.
    """
    import direct_routes as dr
    from db import Contract, ContractSignature

    with SessionLocal() as s:
        made = []

        def add(row):
            s.add(row); s.commit(); made.append(row)
            return row

        draft = add(Contract(tenant_id=w["tid"], program_id=w["prog"],
                             broker_party_id=w["broker"], name="c-draft",
                             lifecycle="draft"))
        agreed_unsigned = add(Contract(tenant_id=w["tid"], program_id=w["prog"],
                                       broker_party_id=w["broker"],
                                       name="c-agreed-unsigned", lifecycle="agreed"))
        agreed_signed = add(Contract(tenant_id=w["tid"], program_id=w["prog"],
                                     broker_party_id=w["broker"],
                                     name="c-agreed-signed", lifecycle="agreed"))
        import datetime as _dt
        add(ContractSignature(tenant_id=w["tid"], contract_id=agreed_signed.id,
                              side="carrier", signer_name="Carrier Admin",
                              method="typed", by_user_id=w["admin_id"],
                              signed_at=_dt.datetime.now(_dt.timezone.utc)))
        s.commit()
        signed_state = add(Contract(tenant_id=w["tid"], program_id=w["prog"],
                                    broker_party_id=w["broker"], name="c-signed",
                                    lifecycle="signed"))
        active = add(Contract(tenant_id=w["tid"], program_id=w["prog"],
                              broker_party_id=w["broker"], name="c-active",
                              lifecycle="active"))
        legacy_null = add(Contract(tenant_id=w["tid"], program_id=w["prog"],
                                   broker_party_id=w["broker"], name="c-legacy",
                                   lifecycle=None))
        try:
            from hierarchy_routes import _contract_settled
            cases = {
                draft.id: False, agreed_unsigned.id: False,
                agreed_signed.id: True, signed_state.id: True,
                active.id: True, legacy_null.id: True,
            }
            for cid, want in cases.items():
                c = s.get(Contract, cid)
                got = _contract_settled(s, c)
                assert got == want, f"{c.name}: expected settled={want}, got {got}"

            # And it has to be the SAME answer _pipeline_ready itself would give
            # for a setup built on each — not merely an answer that looks right.
            class _FakePC:
                def __init__(self, cid): self.contract_id = cid
            for cid, want in cases.items():
                unsettled_names = dr._contracts_not_yet_settled(
                    s, [_FakePC(cid)], w["tid"])
                assert (len(unsettled_names) == 0) == want, s.get(Contract, cid).name
        finally:
            for row in reversed(made):
                s.delete(s.get(type(row), row.id) or row)
            s.commit()
            for sig in (s.query(ContractSignature)
                       .filter(ContractSignature.contract_id == agreed_signed.id)
                       .all()):
                s.delete(sig)
            s.commit()


def test_hierarchy_reports_settled_per_contract(w):
    from db import Contract
    with SessionLocal() as s:
        c = Contract(tenant_id=w["tid"], program_id=w["prog"],
                    broker_party_id=w["broker"], name="c-hier", lifecycle="agreed")
        s.add(c); s.commit()
        cid = c.id
    try:
        client.post(f"/programs/{w['prog']}/brokers",
                    json={"broker_party_id": w["broker"]}, headers=w["admin_h"])
        prog = next(p for p in client.get("/hierarchy", headers=w["admin_h"]).json()
                    ["programmes"] if p["id"] == w["prog"])
        broker_row = next(b for b in prog["brokers"] if b["id"] == w["broker"])
        row = next(c for c in broker_row["contracts"] if c["id"] == cid)
        assert row["lifecycle"] == "agreed"
        assert row["settled"] is False
    finally:
        with SessionLocal() as s:
            c = s.get(Contract, cid)
            if c:
                s.delete(c); s.commit()
            for r in (s.query(ProgramBroker)
                      .filter(ProgramBroker.program_id == w["prog"]).all()):
                s.delete(r)
            s.commit()


def test_hierarchy_flags_the_admins_own_move_on_a_contract(w):
    """awaiting_carrier_admin agrees with contract_routes._carrier_admin_turn —
    the same answer the contract record's banner, the dashboard tile and the
    bell all read. An `agreed`, unsigned contract IS the admin's move; a
    `draft` one is nobody's yet."""
    from db import Contract
    with SessionLocal() as s:
        agreed = Contract(tenant_id=w["tid"], program_id=w["prog"],
                          broker_party_id=w["broker"], name="c-awaiting",
                          lifecycle="agreed")
        draft = Contract(tenant_id=w["tid"], program_id=w["prog"],
                         broker_party_id=w["broker"], name="c-not-yet",
                         lifecycle="draft")
        s.add(agreed); s.add(draft); s.commit()
        agreed_id, draft_id = agreed.id, draft.id
    try:
        client.post(f"/programs/{w['prog']}/brokers",
                    json={"broker_party_id": w["broker"]}, headers=w["admin_h"])
        prog = next(p for p in client.get("/hierarchy", headers=w["admin_h"]).json()
                    ["programmes"] if p["id"] == w["prog"])
        rows = {c["id"]: c for c in
                next(b for b in prog["brokers"] if b["id"] == w["broker"])["contracts"]}
        assert rows[agreed_id]["awaiting_carrier_admin"] is True
        assert rows[agreed_id]["settled"] is False
        assert rows[draft_id]["awaiting_carrier_admin"] is False
    finally:
        with SessionLocal() as s:
            for cid in (agreed_id, draft_id):
                c = s.get(Contract, cid)
                if c:
                    s.delete(c)
            s.commit()
            for r in (s.query(ProgramBroker)
                      .filter(ProgramBroker.program_id == w["prog"]).all()):
                s.delete(r)
            s.commit()

