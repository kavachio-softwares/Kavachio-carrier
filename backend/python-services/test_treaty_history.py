"""Treaty History (treaty_history.py): every step on a treaty, from the
insurer and the reinsurers, read from what is already kept. Pure: in-memory
sqlite, nothing shared.

Run with:  DATABASE_URL=sqlite:// .venv/bin/python -m pytest test_treaty_history.py
"""
import datetime as dt

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import audit_feed as af
import treaty_history as th
from db import (ActivityEvent, AppUser, Contract, ContractApproval, Party, Program, ReinsurerAccessLink,
                ReinsurerApproval, Tenant, Treaty, TreatyLayer, TreatyPartySignature, TreatyProgramme, TreatyShare,
                TreatyTerm)

T = lambda d, h=9: dt.datetime(2026, 11, d, h)  # noqa: E731


class Viewer:
    def __init__(self, broker=False):
        self.is_platform, self.is_broker, self.is_carrier = False, broker, not broker
        self.tenant_id, self.broker_party_id = (None, 50) if broker else (7, None)


class Who:
    user_id, tenant_id, is_platform_admin, role = 1, 7, False, "carrier_admin"


@pytest.fixture
def s(monkeypatch):
    eng = create_engine("sqlite://")
    for m in (Tenant, Party, Program, AppUser, Contract, ContractApproval, ActivityEvent, Treaty, TreatyLayer,
              TreatyShare, TreatyProgramme, ReinsurerAccessLink, ReinsurerApproval, TreatyPartySignature, TreatyTerm):
        m.__table__.create(eng)
    S = sessionmaker(bind=eng)()
    S.add_all([
        Tenant(id=7, tenant_name="ins", legal_name="Insurer Ltd"),
        Party(id=60, party_type="reinsurer", legal_name="Swiss Re"),
        Party(id=61, party_type="reinsurer", legal_name="Hannover Re"),
        Program(id=3, tenant_id=7, name="Property"), Program(id=4, tenant_id=7, name="Marine"),
        AppUser(id=1, email="c@x.test", full_name="Dana", role="carrier_admin", tenant_id=7),
        Contract(id=20, tenant_id=7, contract_type="insurer_reinsurer", name="QS 2026", program_id=3,
                 broker_party_id=60, lifecycle="active", submitted_by_user_id=1, created_at=T(1)),
        Contract(id=21, tenant_id=7, contract_type="insurer_broker", name="Binder", program_id=3, broker_party_id=50),
        Treaty(contract_id=20, tenant_id=7, treaty_type="quota_share", terms={"currency": "GBP"},
               updated_by_user_id=1, created_at=T(1, 10)),
        TreatyLayer(id=1, contract_id=20, tenant_id=7, position=0, name="Whole account"),
        TreatyShare(id=1, layer_id=1, contract_id=20, tenant_id=7, reinsurer_party_id=60, share_pct=60, is_lead=True),
        TreatyShare(id=2, layer_id=1, contract_id=20, tenant_id=7, reinsurer_party_id=61, share_pct=40),
        TreatyProgramme(id=1, contract_id=20, program_id=3, tenant_id=7, created_at=T(1, 10)),
        TreatyProgramme(id=2, contract_id=20, program_id=4, tenant_id=7, created_at=T(2)),
        ContractApproval(tenant_id=7, contract_id=20, action="review_skipped", acted_by_user_id=1, acted_at=T(3)),
        ReinsurerAccessLink(id=1, contract_id=20, tenant_id=7, party_id=60, name="Jane", email="j@x.test",
                            token_hash="h1", expires_at=T(30), sent_at=T(4), sent_by_user_id=1, first_opened_at=T(5)),
        ReinsurerApproval(id=1, contract_id=20, tenant_id=7, party_id=60, status="approved", signer_name="Jane (CEO)",
                          fingerprint="f", at=T(6)),
        ReinsurerApproval(id=2, contract_id=20, tenant_id=7, party_id=61, status="changes_requested",
                          note="Lower the commission", fingerprint="f", at=T(6, 11)),
        TreatyPartySignature(id=1, contract_id=20, tenant_id=7, party_id=60, signer_name="Jane", method="link",
                             signed_at=T(7)),
        TreatyTerm(id=1, contract_id=20, tenant_id=7, term="cession_pct", value=40, effective_from=dt.date(2027, 1, 1),
                   source="manual", created_by_user_id=1, created_at=T(8), note="Addendum 2"),
        # its log twin is skipped; an endorsement has no table of its own
        ActivityEvent(tenant_id=7, action="treaty_term_changed", target="/contracts/20/basis", created_at=T(8)),
        ActivityEvent(tenant_id=7, action="treaty_endorsement_applied", target="/contracts/20/endorsements/5/confirm",
                      actor_user_id=1, created_at=T(9)),
        ActivityEvent(tenant_id=7, action="treaty_structure_saved", target="/contracts/20/treaty",
                      actor_user_id=1, created_at=T(1, 10)),
    ])
    S.commit()
    monkeypatch.setattr(af, "viewer_for", lambda s, p: Viewer(getattr(p, "broker", False)))
    yield S
    S.close()


def test_every_step_from_both_sides_oldest_first(s):
    steps = th.timeline(s, Who(), 20)["steps"]
    assert [x["title"] for x in steps] == [
        "Treaty added", "Structure set: Quota Share, 1 layer, 2 reinsurers", "Now protects Marine",
        "Agreed without a review", "Treaty sent to Swiss Re (Jane)", "Swiss Re opened the treaty",
        "Approved by Swiss Re", "Hannover Re asked for changes", "Signed by Swiss Re (on its link)",
        "Cession changed to 40% from 01 Jan 2027", "Endorsement applied"]
    by = {x["title"]: (x["by"], x["side"]) for x in steps}
    assert by["Approved by Swiss Re"] == ("Jane (CEO) · Swiss Re", "reinsurer")
    assert by["Treaty sent to Swiss Re (Jane)"] == ("Dana", "insurer")
    assert next(x for x in steps if x["title"].startswith("Hannover"))["note"] == "Lower the commission"
    assert next(x for x in steps if x["title"] == "Agreed without a review")["after"] == "Agreed"


def test_each_reinsurer_and_where_it_stands(s):
    t = th.timeline(s, Who(), 20)
    assert t["reinsurers"] == [
        {"id": 60, "name": "Swiss Re", "share": "60%", "lead": True, "standing": "Signed"},
        {"id": 61, "name": "Hannover Re", "share": "40%", "lead": False, "standing": "Asked for changes"}]
    assert t["programmes"] == "Property, Marine" and t["type"] == "Quota Share"


def test_the_list_counts_filters_and_leaves_broker_contracts_out(s):
    out = th.history(s, Who())
    assert out["counts"] == {"treaties": 1, "active": 1, "terms_changed": 1, "waiting": 1}
    assert {i["treaty"] for i in out["items"]} == {"QS 2026"}
    assert [c["name"] for c in out["choices"]["reinsurers"]] == ["Hannover Re", "Swiss Re"]
    assert th.history(s, Who(), reinsurer=61)["total"] == out["total"]          # on its shares
    assert th.history(s, Who(), programme=4)["total"] == out["total"]           # protects Marine too
    assert th.history(s, Who(), since="2026-11-06", until="2026-11-06")["total"] == 2


def test_a_broker_never_sees_treaties(s):
    class Broker(Who):
        broker = True
        role = "broker_admin"
    with pytest.raises(HTTPException) as e:
        th.history(s, Broker())
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        th.timeline(s, Who(), 21)                       # a broker contract is not a treaty
    assert e.value.status_code == 404


def test_each_step_says_where_the_treaty_now_stands():
    import treaty_history as th

    class C:
        lifecycle = "draft"

    def st(action, party=None, after=None):
        return {"action": action, "party": party, "after": after}

    steps = [st("raised"), st("structure"), st("treaty_sent_for_approval"), st("reinsurer_changes", 1),
             st("reinsurer_approved", 1), st("reinsurer_approved", 2), st("reinsurer_signed", 1),
             st("reinsurer_signed", 2), st("signature_withdrawn", 2), st("review_skipped", after="Agreed")]
    th._statuses(C, steps, 2)
    assert [x["after"] for x in steps] == [
        "Draft", None, "Waiting for reinsurers", "Changes requested", "Approved by 1 of 2",
        "Approved by all reinsurers", "Signed by 1 of 2", "Signed by all reinsurers", "Signed by 1 of 2", "Agreed"]


def test_an_uploaded_signed_treaty_starts_active():
    import treaty_history as th

    class C:
        lifecycle = "active"

    steps = [{"action": "raised", "party": None, "after": None}]
    th._statuses(C, steps, 1)
    assert steps[0]["after"] == "Active"
