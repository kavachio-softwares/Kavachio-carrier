"""
Reinsurance treaties, Phase 26 — the structure, the programmes it protects, and
the reinsurer's private read-only link.

What these hold open:

  26.1  each treaty type takes its own terms; a layer cannot be placed past
        100%; only the carrier's reinsurers may sit on a panel; the structure
        stops moving once the terms are settled; the wording follows it.
  26.2  a treaty protects its home programme always and others on request.

  The link  opens nothing without the emailed code; shows one reinsurer its own
            line and nobody else's; and stops opening — saying why — when it is
            revoked, when the treaty ends, and when the contact is removed.
"""
import os
os.environ["MAIL_ALLOWED_RECIPIENTS"] = "nobody@example.invalid"
os.environ.setdefault("APP_BASE_URL", "http://localhost:5173")
import pytest
from fastapi.testclient import TestClient

import main
import treaty_routes
import treaty_types as tt
from auth_tokens import mint_access_token
from db import (AppUser, Contract, Party, PartyContact, Program, SessionLocal,
                Tenant)

client = TestClient(main.app)

TERM = {"inception_dt": "2027-01-01", "expiry_dt": "2099-12-31"}


@pytest.fixture(scope="module")
def w():
    sfx = os.urandom(4).hex()
    with SessionLocal() as s:
        car = Tenant(tenant_name=f"treaty-{sfx}", legal_name="Insurisk Specialty")
        s.add(car); s.commit()
        ids = {}
        for key, name in (("swiss", "Swiss Re"), ("munich", "Munich Re"),
                          ("hann", "Hannover Re")):
            pt = Party(tenant_id=car.id, party_type="reinsurer", legal_name=name,
                       reference=f"tr-{key}-{sfx}", is_active=True)
            s.add(pt); s.commit()
            ids[key] = pt.id
        br = Party(tenant_id=car.id, party_type="broker", legal_name="CRC",
                   reference=f"tr-b-{sfx}", is_active=True)
        s.add(br); s.commit()
        p1 = Program(tenant_id=car.id, name="Spectrum Property", is_app_managed=True)
        p2 = Program(tenant_id=car.id, name="Spectrum Casualty", is_app_managed=True)
        s.add(p1); s.commit(); s.add(p2); s.commit()
        f = (s.query(AppUser).filter(AppUser.role == "kavachio_admin")
             .order_by(AppUser.id).first())
        dana = AppUser(tenant_id=car.id, email=f"d-{sfx}@x.test", full_name="Dana",
                       role="carrier_admin", invited_by_user_id=f.id)
        s.add(dana); s.commit()
        car.owner_user_id = dana.id if hasattr(car, "owner_user_id") else None
        s.commit()
        swiss_c = PartyContact(party_id=ids["swiss"], tenant_id=car.id,
                               full_name="Sam Swiss", email=f"sam-{sfx}@swiss.test")
        munich_c = PartyContact(party_id=ids["munich"], tenant_id=car.id,
                                full_name="Mia Munich", email=f"mia-{sfx}@munich.test")
        hann_c = PartyContact(party_id=ids["hann"], tenant_id=car.id,
                              full_name="Hal Hann", email=f"hal-{sfx}@hann.test")
        s.add_all([swiss_c, munich_c, hann_c]); s.commit()
        return {"t": car.id, "b": br.id, "p1": p1.id, "p2": p2.id, **ids,
                "c_swiss": swiss_c.id, "c_munich": munich_c.id,
                "c_hann": hann_c.id,
                "ch": {"Authorization":
                       f"Bearer {mint_access_token(dana.id, car.id, 'carrier_admin')}"}}


def _xol(w, *, second_layer_munich=40.0):
    return {
        "treaty_type": "xol",
        "terms": {"currency": "usd", "cover_basis": "per_event",
                  "basis": "losses_occurring",
                  "placement_reference": "GRE-2027-0418"},
        "layers": [
            {"name": "Layer 1", "terms": {"retention": 1_000_000,
                                          "limit": 4_000_000,
                                          "reinstatements": 1,
                                          "rate_on_line_pct": 12},
             "shares": [
                 {"reinsurer_party_id": w["swiss"], "share_pct": 60, "is_lead": True},
                 {"reinsurer_party_id": w["munich"], "share_pct": 40}]},
            {"name": "Layer 2", "terms": {"retention": 5_000_000,
                                          "limit": 10_000_000},
             "shares": [
                 {"reinsurer_party_id": w["swiss"], "share_pct": 50, "is_lead": True},
                 {"reinsurer_party_id": w["munich"],
                  "share_pct": second_layer_munich}]},
        ],
    }


def _create(w, **over):
    body = {"program_id": w["p1"], "contract_type": "insurer_reinsurer",
            "counterparty_party_id": w["swiss"], "name": "Property Cat XL 2027",
            "year_of_account": "2027", "notice_period_days": 90,
            "create_as": "draft", "treaty": _xol(w), **TERM}
    body.update(over)
    return client.post("/contracts", headers=w["ch"], json=body)


def _set_state(cid, state):
    with SessionLocal() as s:
        c = s.get(Contract, cid)
        c.lifecycle = state
        s.commit()


# ── 26.1 the structure ──────────────────────────────────────────────────────
def test_the_spec_offers_the_five_types(w):
    r = client.get("/treaty-types", headers=w["ch"])
    assert r.status_code == 200, r.text
    types = {t["key"]: t for t in r.json()["types"]}
    assert set(types) == {"quota_share", "surplus", "xol", "stop_loss",
                          "facultative"}
    assert types["xol"]["multi_layer"] is True
    assert not types["quota_share"]["multi_layer"]
    req = {f["name"] for f in types["quota_share"]["fields"] if f["required"]}
    assert "cession_pct" in req
    # Stop loss asks for loss-ratio OR amount figures, never both at once.
    sl = {f["name"]: f for f in types["stop_loss"]["layer_fields"]}
    assert sl["attachment_pct"]["when"] == {"field": "measure",
                                            "equals": "loss_ratio"}


def test_a_treaty_is_created_with_its_structure_and_summary(w):
    r = _create(w, program_ids=[w["p2"]])
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["treaty"]["treaty_type"] == "xol"
    assert rec["treaty"]["type_label"] == "Excess of Loss"
    # 14m xs 1m, the top of the tower minus the bottom retention.
    assert rec["treaty"]["headline"].startswith("USD 14m xs USD 1m")
    # Layer 2 is 90% placed, so the treaty is too.
    assert rec["treaty"]["placed_pct"] == 90
    assert rec["treaty"]["fully_placed"] is False
    progs = {p["id"]: p for p in rec["treaty"]["programmes"]}
    assert set(progs) == {w["p1"], w["p2"]}
    assert progs[w["p1"]]["is_home"]

    t = client.get(f"/contracts/{rec['id']}/treaty", headers=w["ch"]).json()
    bands = t["summary"]["tower"]["bands"]
    assert [b["kind"] for b in bands] == ["retention", "layer", "layer"]
    assert bands[2]["layer"] == 1 and bands[2]["placed_pct"] == 90
    assert t["structure"]["terms"]["currency"] == "USD"
    assert t["can_edit"] is True
    names = {sh["reinsurer_name"] for sh in t["structure"]["layers"][0]["shares"]}
    assert names == {"Swiss Re", "Munich Re"}


def test_a_layer_placed_past_100_percent_is_refused(w):
    r = _create(w, treaty=_xol(w, second_layer_munich=70))
    assert r.status_code == 400, r.text
    assert "layers.1.shares" in r.json()["detail"]["errors"]


def test_only_a_reinsurer_can_sit_on_a_panel(w):
    bad = _xol(w)
    bad["layers"][0]["shares"][1]["reinsurer_party_id"] = w["b"]
    r = _create(w, treaty=bad)
    assert r.status_code == 400, r.text
    assert "layers.0.shares.1.reinsurer_party_id" in r.json()["detail"]["errors"]


def test_each_type_takes_its_own_required_terms(w):
    r = _create(w, treaty={"treaty_type": "quota_share",
                           "terms": {"currency": "USD"}})
    assert r.status_code == 400
    assert "terms.cession_pct" in r.json()["detail"]["errors"]
    r = _create(w, treaty={"treaty_type": "quota_share",
                           "terms": {"currency": "USD", "cession_pct": 40,
                                     "ceding_commission_pct": 30}})
    assert r.status_code == 200, r.text
    assert r.json()["treaty"]["headline"] == "40% quota share"


def test_xol_gaps_between_layers_are_warned_about(w):
    st = tt.validate(_xol(w) | {"layers": [
        {"name": "L1", "terms": {"retention": 1e6, "limit": 2e6}},
        {"name": "L2", "terms": {"retention": 5e6, "limit": 5e6}}]})
    summ = tt.summarise(st)
    assert any("gap" in x for x in summ["warnings"])


def test_the_wording_carries_the_treaty_and_follows_it(w):
    pre = client.post("/contract-wording/preview", headers=w["ch"], json={
        "contract_type": "insurer_reinsurer",
        "values": {"name": "T", **TERM}, "agreed_limits": {},
        "carrier_name": "Insurisk", "counterparty_name": "Swiss Re",
        "treaty": _xol(w)})
    assert pre.status_code == 200, pre.text
    secs = pre.json()["sections"]
    keys = [x["key"] for x in secs]
    assert keys[0] == "parties" and "treaty_layers" in keys
    assert "Reinsured" in secs[0]["rendered"]
    # The panel is confidential: no reinsurer is named in the treaty clauses.
    treaty_text = " ".join(x["rendered"] for x in secs
                           if x["key"].startswith("treaty_"))
    assert "Munich" not in treaty_text and "Swiss" not in treaty_text

    r = _create(w, wording_sections=[{k: v for k, v in x.items()
                                      if k in ("key", "title", "body", "origin")}
                                     for x in secs])
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    changed = _xol(w)
    changed["layers"][0]["terms"]["limit"] = 9_000_000
    u = client.put(f"/contracts/{cid}/treaty", headers=w["ch"], json=changed)
    assert u.status_code == 200, u.text
    rec = client.get(f"/contracts/{cid}", headers=w["ch"]).json()
    layers = next(x for x in rec["wording_sections"]
                  if x["key"] == "treaty_layers")
    assert "9,000,000" in layers["rendered"]


def test_the_structure_is_fixed_once_the_terms_are_settled(w):
    cid = _create(w).json()["id"]
    _set_state(cid, "agreed")
    r = client.put(f"/contracts/{cid}/treaty", headers=w["ch"], json=_xol(w))
    assert r.status_code == 409, r.text
    t = client.get(f"/contracts/{cid}/treaty", headers=w["ch"]).json()
    assert t["can_edit"] is False and t["edit_blocked_reason"]
    # …but which programmes it protects can still change mid-term.
    r = client.put(f"/contracts/{cid}/treaty/programmes", headers=w["ch"],
                   json={"program_ids": [w["p2"]]})
    assert r.status_code == 200, r.text
    assert {p["id"] for p in r.json()["programmes"]} == {w["p1"], w["p2"]}


def test_a_treaty_signature_needs_the_signed_copy(w):
    """The reinsurer signs outside Kavachio, so its signature is recorded with
    the document that came back — never on a typed name alone."""
    cid = _create(w).json()["id"]
    _set_state(cid, "agreed")
    with SessionLocal() as s:
        c = s.get(Contract, cid)
        c.wording_sections = {"sections": [{"key": "parties", "title": "P",
                                            "body": "1.1  x"}]}
        s.commit()
    r = client.post(f"/contracts/{cid}/sign", headers=w["ch"], json={
        "recorded": True, "side": "counterparty", "signer_name": "Ana Swiss"})
    assert r.status_code == 400, r.text
    assert "document_id" in r.json()["detail"]["errors"]


def test_a_renewal_carries_the_structure_forward(w):
    import datetime as _dt
    # Renewal opens only in the last days of a term, so this one ends soon.
    today = _dt.date.today()
    end = today + _dt.timedelta(days=10)
    cid = _create(w, program_ids=[w["p2"]],
                  inception_dt=(today - _dt.timedelta(days=300)).isoformat(),
                  expiry_dt=end.isoformat()).json()["id"]
    _set_state(cid, "active")
    r = client.post(f"/contracts/{cid}/renew", headers=w["ch"], json={
        "inception_dt": (end + _dt.timedelta(days=1)).isoformat(),
        "expiry_dt": (end + _dt.timedelta(days=366)).isoformat()})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["treaty"]["treaty_type"] == "xol"
    assert {p["id"] for p in new["treaty"]["programmes"]} == {w["p1"], w["p2"]}


# ── 26.2 programmes ─────────────────────────────────────────────────────────
def test_programmes_attach_detach_and_count(w):
    cid = _create(w).json()["id"]
    r = client.post(f"/programs/{w['p2']}/treaties", headers=w["ch"],
                    json={"contract_id": cid})
    assert r.status_code == 200, r.text
    assert cid in [t["contract_id"] for t in r.json()["treaties"]]

    counts = client.get("/treaty-counts", headers=w["ch"]).json()["counts"]
    assert counts[str(w["p2"])] >= 1

    r = client.delete(f"/programs/{w['p1']}/treaties/{cid}", headers=w["ch"])
    assert r.status_code == 409, "the home programme is always protected"
    r = client.delete(f"/programs/{w['p2']}/treaties/{cid}", headers=w["ch"])
    assert r.status_code == 200
    assert cid not in [t["contract_id"] for t in r.json()["treaties"]]
    assert cid in [t["contract_id"] for t in r.json()["attachable"]]


def test_the_treaty_type_filters_the_contract_list(w):
    _create(w)
    r = client.get("/contracts", headers=w["ch"],
                   params={"treaty_type": "xol", "page": 1, "page_size": 50})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items and all(i["treaty"]["treaty_type"] == "xol" for i in items)


# ── the reinsurer's private link ────────────────────────────────────────────
@pytest.fixture
def sent(monkeypatch):
    """Captures what would have been emailed: (link_id, token, code)."""
    out = []

    def fake(s, link, c, token, code, **kw):
        out.append((link.id, token, code))
        return True
    monkeypatch.setattr(treaty_routes, "_send_link_email", fake)
    return out


def _share(w, cid, contacts):
    return client.post(f"/contracts/{cid}/reinsurer-access", headers=w["ch"],
                       json={"contact_ids": contacts})


def _unlock(token, code):
    r = client.post(f"/treaty-view/{token}/verify", json={"code": code})
    assert r.status_code == 200, r.text
    return {"X-Treaty-Session": r.json()["session"]}


def test_only_contacts_at_reinsurers_on_the_panel_can_be_sent_a_link(w, sent):
    cid = _create(w).json()["id"]
    r = _share(w, cid, [w["c_hann"]])     # Hannover holds no share
    assert r.status_code == 400, r.text
    access = client.get(f"/contracts/{cid}/reinsurer-access", headers=w["ch"]).json()
    assert {g["party_id"] for g in access["contacts"]} == {w["swiss"], w["munich"]}


def test_the_link_opens_nothing_without_the_code(w, sent):
    cid = _create(w).json()["id"]
    assert _share(w, cid, [w["c_munich"]]).status_code == 200
    _lid, token, code = sent[-1]
    r = client.get(f"/treaty-view/{token}")
    assert r.status_code == 200
    body = r.json()
    assert body["locked"] is True
    assert "layers" not in body and "summary" not in body
    assert body["email_hint"].endswith("@munich.test")

    bad = "000000" if code != "000000" else "111111"
    r = client.post(f"/treaty-view/{token}/verify", json={"code": bad})
    assert r.status_code == 400 and "attempts left" in r.json()["detail"]

    hdr = _unlock(token, code)
    view = client.get(f"/treaty-view/{token}", headers=hdr).json()
    assert view["locked"] is False


def test_a_reinsurer_sees_its_own_lines_and_nobody_elses(w, sent):
    cid = _create(w).json()["id"]
    _share(w, cid, [w["c_munich"]])
    _lid, token, code = sent[-1]
    view = client.get(f"/treaty-view/{token}", headers=_unlock(token, code)).json()
    assert view["viewer"]["organisation"] == "Munich Re"
    assert [lay["your_share"]["share_pct"] for lay in view["layers"]] == [40, 40]
    assert view["layers"][1]["placed_pct"] == 90
    assert "Swiss" not in str(view)
    assert view["summary"]["tower"]["bands"]


def test_a_session_from_one_link_does_not_open_another(w, sent):
    cid = _create(w).json()["id"]
    _share(w, cid, [w["c_munich"], w["c_swiss"]])
    (_a, tok_a, code_a), (_b, tok_b, _code_b) = sent[-2], sent[-1]
    hdr = _unlock(tok_a, code_a)
    assert client.get(f"/treaty-view/{tok_b}", headers=hdr).json()["locked"] is True


def test_five_wrong_codes_lock_the_link(w, sent):
    cid = _create(w).json()["id"]
    _share(w, cid, [w["c_swiss"]])
    _lid, token, code = sent[-1]
    bad = "000000" if code != "000000" else "111111"
    statuses = [client.post(f"/treaty-view/{token}/verify",
                            json={"code": bad}).status_code for _ in range(5)]
    assert statuses[-1] == 429
    # Locked: even the right code is refused until the lockout passes.
    assert client.post(f"/treaty-view/{token}/verify",
                       json={"code": code}).status_code == 429


def test_revoking_closes_the_link_and_says_so(w, sent):
    cid = _create(w).json()["id"]
    _share(w, cid, [w["c_swiss"]])
    lid, token, _code = sent[-1]
    r = client.post(f"/contracts/{cid}/reinsurer-access/{lid}/revoke",
                    headers=w["ch"])
    assert r.status_code == 200
    assert r.json()["links"][0]["status"] == "revoked"
    view = client.get(f"/treaty-view/{token}").json()
    assert view["closed"] is True and view["title"] == "This link has been withdrawn"


def test_resending_kills_the_old_url(w, sent):
    cid = _create(w).json()["id"]
    _share(w, cid, [w["c_swiss"]])
    lid, old_token, _ = sent[-1]
    r = client.post(f"/contracts/{cid}/reinsurer-access/{lid}/resend",
                    headers=w["ch"])
    assert r.status_code == 200, r.text
    assert client.get(f"/treaty-view/{old_token}").status_code == 404
    _lid, new_token, _ = sent[-1]
    assert client.get(f"/treaty-view/{new_token}").json()["locked"] is True


def test_a_terminated_treaty_closes_every_link(w, sent):
    cid = _create(w).json()["id"]
    _share(w, cid, [w["c_swiss"]])
    _lid, token, _ = sent[-1]
    _set_state(cid, "terminated")
    view = client.get(f"/treaty-view/{token}").json()
    assert view["closed"] is True and "terminated" in view["title"]


def test_removing_the_contact_closes_their_link(w, sent):
    cid = _create(w).json()["id"]
    with SessionLocal() as s:
        extra = PartyContact(party_id=w["swiss"], tenant_id=w["t"],
                             full_name="Temp", email="temp@swiss.test")
        s.add(extra); s.commit()
        extra_id = extra.id
    _share(w, cid, [extra_id])
    _lid, token, _ = sent[-1]
    with SessionLocal() as s:
        s.delete(s.get(PartyContact, extra_id)); s.commit()
    view = client.get(f"/treaty-view/{token}").json()
    assert view["closed"] is True and "contact" in view["title"]


def test_the_access_history_records_what_happened(w, sent):
    cid = _create(w).json()["id"]
    _share(w, cid, [w["c_munich"]])
    _lid, token, code = sent[-1]
    client.get(f"/treaty-view/{token}", headers=_unlock(token, code))
    events = client.get(f"/contracts/{cid}/reinsurer-access",
                        headers=w["ch"]).json()["events"]
    assert {"sent", "unlocked", "viewed"} <= {e["type"] for e in events}
    link = client.get(f"/contracts/{cid}/reinsurer-access",
                      headers=w["ch"]).json()["links"][0]
    assert link["first_opened_at"] and link["status"] == "active"


def test_a_reinsurer_page_lists_its_treaties_and_lines(w):
    cid = _create(w).json()["id"]
    r = client.get(f"/reinsurers/{w['munich']}/treaties", headers=w["ch"])
    assert r.status_code == 200, r.text
    row = next(t for t in r.json()["treaties"] if t["contract_id"] == cid)
    assert row["is_lead_reinsurer"] is False
    assert [ln["share_pct"] for ln in row["lines"]] == [40, 40]


def test_the_starred_lead_is_who_the_treaty_is_with(w):
    """No separate reinsurer picker on a treaty: the panel's lead decides."""
    body = _xol(w)
    for lay in body["layers"]:
        for sh in lay["shares"]:
            sh["is_lead"] = sh["reinsurer_party_id"] == w["munich"]
    r = _create(w, counterparty_party_id=None, treaty=body)
    assert r.status_code == 200, r.text
    assert r.json()["counterparty"]["id"] == w["munich"]
    # …and moving the star moves the counterparty.
    cid = r.json()["id"]
    body["layers"][0]["shares"][0]["is_lead"] = True
    body["layers"][0]["shares"][1]["is_lead"] = False
    assert client.put(f"/contracts/{cid}/treaty", headers=w["ch"],
                      json=body).status_code == 200
    rec = client.get(f"/contracts/{cid}", headers=w["ch"]).json()
    assert rec["counterparty"]["id"] == w["swiss"]


def test_the_dashboard_counts_what_kavachio_holds(w, sent):
    cid = _create(w).json()["id"]           # a draft, 90% placed
    _share(w, cid, [w["c_munich"]])
    r = client.get("/reinsurance/dashboard", headers=w["ch"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["live"] >= 1 and d["counts"]["under_placed"] >= 1
    assert d["links"]["active"] >= 1 and d["links"]["never_opened"] >= 1
    assert any(a["contract_id"] == cid and a["tab"] == "treaty"
               for a in d["actions"]) or d["actions_total"] > 8
    assert d["programmes"]["protected"] >= 1
    b = d["bordereaux"]
    assert b["total"] == b["clean"] + b["with_exceptions"] + b["not_validated"]


def test_the_dashboard_lists_treaties_about_to_expire(w):
    import datetime as _dt
    soon = (_dt.date.today() + _dt.timedelta(days=30)).isoformat()
    cid = _create(w, inception_dt="2020-01-01", expiry_dt=soon).json()["id"]
    _set_state(cid, "active")
    d = client.get("/reinsurance/dashboard", headers=w["ch"]).json()
    row = next(e for e in d["expiring"] if e["contract_id"] == cid)
    assert row["days_left"] == 30 and row["renewal_id"] is None


def test_a_signed_treaty_is_uploaded_and_put_in_force(w):
    import json as _j
    r = client.post("/treaties/upload", headers=w["ch"], data={
        "program_id": w["p1"], "name": "Signed QS 2027",
        "inception_dt": "2027-01-01", "expiry_dt": "2099-12-31",
        "year_of_account": "2027", "notice_period_days": 90,
        "treaty": _j.dumps({"treaty_type": "quota_share",
                            "terms": {"currency": "USD", "cession_pct": 30},
                            "layers": [{"name": "QS", "shares": [
                                {"reinsurer_party_id": w["hann"], "share_pct": 100,
                                 "is_lead": True}]}]}),
        "program_ids": _j.dumps([w["p2"]]),
    }, files={"file": ("signed-slip.pdf", b"%PDF-1.4 signed", "application/pdf")})
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["lifecycle"] == "active"
    assert rec["counterparty"]["id"] == w["hann"]
    assert rec["is_uploaded"] is True
    docs = [d for d in rec["documents"] if d["kind"] == "contract"]
    assert docs and docs[0]["is_executed_copy"]
    # Signed means fixed: its shares and terms cannot be edited on the record.
    t = client.get(f"/contracts/{rec['id']}/treaty", headers=w["ch"]).json()
    assert t["can_edit"] is False and t["can_edit_programmes"] is True


def test_an_upload_without_a_starred_lead_is_refused(w):
    import json as _j
    r = client.post("/treaties/upload", headers=w["ch"], data={
        "program_id": w["p1"], "name": "No lead", "inception_dt": "2027-01-01",
        "expiry_dt": "2099-12-31", "year_of_account": "2027",
        "notice_period_days": 90,
        "treaty": _j.dumps({"treaty_type": "quota_share",
                            "terms": {"currency": "USD", "cession_pct": 30}}),
    }, files={"file": ("slip.pdf", b"%PDF-1.4", "application/pdf")})
    assert r.status_code == 400, r.text


def test_the_reader_fills_the_form_from_the_document():
    """The model call is stubbed: this checks what is done with its answer —
    cleaned to the vocabulary, reinsurers matched by name, unknown ones listed,
    and nothing invented."""
    import json as _j
    import treaty_reader
    answer = {
        "name": "Property Cat XL 2027", "inception_dt": "2027-01-01",
        "expiry_dt": "2027-12-31", "year_of_account": "2027",
        "notice_period_days": "90", "executed_date": "not a date",
        "treaty_type": "xol",
        "terms": {"currency": "usd", "cover_basis": "per_event",
                  "made_up_field": 5},
        "layers": [{"name": "Layer 1",
                    "terms": {"retention": 1000000, "limit": 4000000},
                    "shares": [
                        {"reinsurer": "Munich Reinsurance Company", "share_pct": 40},
                        {"reinsurer": "Swiss Re Ltd", "share_pct": 50},
                        {"reinsurer": "Scor SE", "share_pct": 10}]}],
    }
    parties = [(1, "Swiss Re"), (2, "Munich Re"), (3, "Hannover Re")]
    out = treaty_reader.read_treaty("some text", parties,
                                    call=lambda prompt: _j.dumps(answer))
    assert out["name"] == "Property Cat XL 2027"
    assert out["notice_period_days"] == 90
    assert out["executed_date"] is None
    t = out["treaty"]
    assert t["treaty_type"] == "xol"
    assert t["terms"] == {"currency": "USD", "cover_basis": "per_event"}
    ids = [s["reinsurer_party_id"] for s in t["layers"][0]["shares"]]
    assert ids == [2, 1, None]
    assert out["unmatched_reinsurers"] == ["Scor SE"]
    # No leader named: the biggest line leads, for the carrier to move.
    assert [s["is_lead"] for s in t["layers"][0]["shares"]] == [False, True, False]


def test_reading_a_file_with_no_text_says_so(w, monkeypatch):
    import contract_routes
    monkeypatch.setattr(contract_routes, "_parse_to_text", lambda d, n: "  ")
    r = client.post("/treaties/read", headers=w["ch"],
                    files={"file": ("scan.pdf", b"%PDF", "application/pdf")})
    assert r.status_code == 400 and "by hand" in r.json()["detail"]


# ── reinsurers approve on their private link ────────────────────────────────
def _links_for(w, cid, sent, contacts):
    _share(w, cid, contacts)
    out = {}
    for lid, token, code in sent[-len(contacts):]:
        out[lid] = (token, _unlock(token, code))
    return list(out.values())


def test_every_reinsurer_approving_agrees_the_terms(w, sent):
    cid = _create(w).json()["id"]                       # Swiss lead, Munich
    (t_swiss, h_swiss), (t_munich, h_munich) = _links_for(
        w, cid, sent, [w["c_swiss"], w["c_munich"]])

    r = client.post(f"/treaty-view/{t_swiss}/approve", headers=h_swiss,
                    json={"signer_name": "Sam Weber", "signer_title": "Underwriter"})
    assert r.status_code == 200, r.text
    assert r.json()["terms_agreed"] is False
    rec = client.get(f"/contracts/{cid}", headers=w["ch"]).json()
    assert rec["lifecycle"] == "draft"
    assert rec["treaty"]["approvals"]["approved"] == 1

    r = client.post(f"/treaty-view/{t_munich}/approve", headers=h_munich,
                    json={"signer_name": "Mia Huber"})
    assert r.json()["terms_agreed"] is True
    rec = client.get(f"/contracts/{cid}", headers=w["ch"]).json()
    assert rec["lifecycle"] == "agreed"
    access = client.get(f"/contracts/{cid}/reinsurer-access", headers=w["ch"]).json()
    assert {p["status"] for p in access["approvals"]["parties"]} == {"approved"}


def test_changing_the_treaty_makes_an_approval_stale(w, sent):
    cid = _create(w).json()["id"]
    ((t, h),) = _links_for(w, cid, sent, [w["c_munich"]])
    client.post(f"/treaty-view/{t}/approve", headers=h, json={"signer_name": "Mia"})
    changed = _xol(w)
    changed["layers"][0]["terms"]["limit"] = 9_000_000
    assert client.put(f"/contracts/{cid}/treaty", headers=w["ch"],
                      json=changed).status_code == 200
    a = client.get(f"/contracts/{cid}/reinsurer-access", headers=w["ch"]).json()["approvals"]
    munich = next(p for p in a["parties"] if p["party_id"] == w["munich"])
    assert munich["status"] == "stale" and munich["last_status"] == "approved"
    view = client.get(f"/treaty-view/{t}", headers=h).json()
    assert view["approval"]["status"] == "stale" and view["approval"]["can_answer"]


def test_a_reinsurer_can_ask_for_a_change(w, sent):
    cid = _create(w).json()["id"]
    ((t, h),) = _links_for(w, cid, sent, [w["c_munich"]])
    r = client.post(f"/treaty-view/{t}/request-changes", headers=h,
                    json={"note": "Our line on layer 2 is 20%, not 40%."})
    assert r.status_code == 200, r.text
    a = client.get(f"/contracts/{cid}/reinsurer-access", headers=w["ch"]).json()["approvals"]
    munich = next(p for p in a["parties"] if p["party_id"] == w["munich"])
    assert munich["status"] == "changes_requested" and "20%" in munich["note"]
    assert client.get(f"/contracts/{cid}", headers=w["ch"]).json()["lifecycle"] == "draft"


def test_approving_needs_the_code(w, sent):
    cid = _create(w).json()["id"]
    _share(w, cid, [w["c_munich"]])
    _lid, token, _code = sent[-1]
    r = client.post(f"/treaty-view/{token}/approve", json={"signer_name": "Mia"})
    assert r.status_code == 401


def test_an_expired_treaty_can_still_be_approved(w, sent):
    cid = _create(w, inception_dt="2020-01-01", expiry_dt="2020-12-31").json()["id"]
    _set_state(cid, "active")                    # in force, past its expiry
    ((t, h),) = _links_for(w, cid, sent, [w["c_munich"]])
    view = client.get(f"/treaty-view/{t}", headers=h).json()
    assert view["approval"]["can_answer"] is True
    r = client.post(f"/treaty-view/{t}/approve", headers=h, json={"signer_name": "Mia"})
    assert r.status_code == 200, r.text


def test_a_signed_treaty_can_only_be_approved(w, sent):
    cid = _create(w).json()["id"]
    _set_state(cid, "active")
    ((t, h),) = _links_for(w, cid, sent, [w["c_munich"]])
    a = client.get(f"/treaty-view/{t}", headers=h).json()["approval"]
    assert a["can_answer"] is True and a["can_request_changes"] is False
    r = client.post(f"/treaty-view/{t}/request-changes", headers=h,
                    json={"note": "change it"})
    assert r.status_code == 409, r.text
    assert client.post(f"/treaty-view/{t}/approve", headers=h,
                       json={"signer_name": "Mia"}).status_code == 200


def test_a_treaty_never_opens_an_electronic_round(w):
    cid = _create(w).json()["id"]
    _set_state(cid, "agreed")
    r = client.get(f"/esign/contracts/{cid}/round", headers=w["ch"]).json()
    assert r["can_sign"] is False and r["treaty"] is True
    r = client.post(f"/esign/contracts/{cid}/signing-session", headers=w["ch"])
    assert r.status_code == 409 and "signature page" in r.json()["detail"]


# ── the lead reinsurer signs on its link ────────────────────────────────────
def _carrier_signed(w):
    cid = _create(w).json()["id"]                       # Swiss is the lead
    _set_state(cid, "agreed")
    with SessionLocal() as s:
        c = s.get(Contract, cid)
        c.wording_sections = {"sections": [{"key": "parties", "title": "P",
                                            "body": "1.1  x"}]}
        s.commit()
    return cid


def test_a_signing_link_waits_for_the_carrier_to_sign_first(w, sent):
    cid = _carrier_signed(w)
    r = client.post(f"/contracts/{cid}/reinsurer-access", headers=w["ch"],
                    json={"contact_ids": [w["c_swiss"]], "sign": True})
    assert r.status_code == 409 and "carrier signs first" in r.text


def test_only_the_lead_reinsurer_is_asked_to_sign(w, sent):
    cid = _carrier_signed(w)
    client.post(f"/contracts/{cid}/sign", headers=w["ch"],
                json={"signer_name": "Dana"})
    r = client.post(f"/contracts/{cid}/reinsurer-access", headers=w["ch"],
                    json={"contact_ids": [w["c_munich"]], "sign": True})
    assert r.status_code == 400 and "lead reinsurer" in r.text


def test_the_lead_reinsurer_signs_on_its_link_and_the_treaty_goes_live(w, sent):
    cid = _carrier_signed(w)
    r = client.post(f"/contracts/{cid}/sign", headers=w["ch"],
                    json={"signer_name": "Dana"})
    assert r.status_code == 200, r.text
    acc = client.get(f"/contracts/{cid}/reinsurer-access", headers=w["ch"]).json()
    assert acc["signing"]["can_request"] is True

    # Munich's plain link carries no signature box.
    ((t_m, h_m),) = _links_for(w, cid, sent, [w["c_munich"]])
    assert client.get(f"/treaty-view/{t_m}", headers=h_m).json()["signature"] is None
    assert client.post(f"/treaty-view/{t_m}/sign", headers=h_m, json={
        "signer_name": "Mia", "consent": True}).status_code == 403

    r = client.post(f"/contracts/{cid}/reinsurer-access", headers=w["ch"],
                    json={"contact_ids": [w["c_swiss"]], "sign": True})
    assert r.status_code == 200, r.text
    lid, token, code = sent[-1]
    # Not without the code.
    assert client.post(f"/treaty-view/{token}/sign", json={
        "signer_name": "Sam Swiss", "consent": True}).status_code == 401
    h = _unlock(token, code)
    view = client.get(f"/treaty-view/{token}", headers=h).json()
    assert view["signature"]["can_sign"] is True
    assert client.post(f"/treaty-view/{token}/sign", headers=h, json={
        "signer_name": "Sam Swiss"}).status_code == 400            # no consent
    r = client.post(f"/treaty-view/{token}/sign", headers=h, json={
        "signer_name": "Sam Swiss", "signer_title": "Underwriter", "consent": True})
    assert r.status_code == 200, r.text
    assert r.json()["in_force"] is True
    assert r.json()["view"]["signature"]["signed"]["name"] == "Sam Swiss"

    rec = client.get(f"/contracts/{cid}", headers=w["ch"]).json()
    assert rec["lifecycle"] == "active"
    theirs = [g for g in rec["signatures"] if g["side"] == "counterparty"]
    assert len(theirs) == 1 and theirs[0]["method"] == "link"
    assert client.post(f"/treaty-view/{token}/sign", headers=h, json={
        "signer_name": "Sam Swiss", "consent": True}).status_code == 409
    acc = client.get(f"/contracts/{cid}/reinsurer-access", headers=w["ch"]).json()
    mine = next(x for x in acc["links"] if x["id"] == lid)
    assert mine["sign_requested_at"] and mine["signed_at"]
    assert any(e["type"] == "signed" for e in acc["events"])
