"""An uploaded output layout is filed under the programme, broker and contract
it is for.

THE GAP. Bordereau Setup uploaded its sample output BDX with the carrier only.
Every uploaded layout was saved with no programme and no broker, so the lookup
could reach it only through a setup, and a second broker's upload under the
programme's name became "version 2" of the first broker's layout.

What must now hold:
  · programme and broker (and the contract, when one is sent) are saved
  · a scope the hierarchy does not contain is refused before the file is read
    or the AI is asked: a broker not on the programme, another broker's
    contract, a contract on another programme, a broker or contract with no
    programme
  · each broker's lookup finds its own uploaded layout, never the other's
  · the old call with no programme still saves, untied, as before

    python -m pytest test_output_template_upload_scope.py
"""
from __future__ import annotations

import io
import os

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

import main  # noqa: F401 — loads .env, which db needs at import
from auth_tokens import mint_access_token
from db import (
    ActivityEvent, AppUser, Contract, ExportTemplate, Party, Program,
    ProgramBroker, SessionLocal, Tenant,
)
from output_template_routes import _resolve

client = TestClient(main.app)


def _sample() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Risk BDX"
    ws.append(["UMR", "Certificate Ref", "Gross Premium"])
    ws.append(["B0001", "C-1", 1000])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture()
def ai_calls(monkeypatch):
    """No AI in a test: the column proposal is stubbed, and counted, so a
    refusal can be shown to come before it."""
    calls: list[int] = []
    monkeypatch.setattr(main, "propose_template_mapping", lambda structure: calls.append(1))
    return calls


@pytest.fixture()
def w():
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

        carrier = add(Tenant(tenant_name=f"ous-{sfx}", legal_name="Upload Scope Carrier"))
        cpid = _ensure_carrier_party(s, carrier.id); s.commit()
        made.append((Party, cpid))
        prog = add(Program(tenant_id=carrier.id, name=f"OUS {sfx}", is_app_managed=True))
        other_prog = add(Program(tenant_id=carrier.id, name=f"OUS other {sfx}",
                                 is_app_managed=True))
        brokers = {}
        for key in ("a", "b", "off"):
            b = add(Party(tenant_id=carrier.id, party_type="broker",
                          legal_name=f"Upload Broker {key}", reference=f"ous-{key}-{sfx}"))
            brokers[key] = b.id
            if key != "off":                      # "off" is never put on the programme
                add(ProgramBroker(tenant_id=carrier.id, program_id=prog.id,
                                  broker_party_id=b.id, status="active"))
        contract_b = add(Contract(tenant_id=carrier.id, program_id=prog.id,
                                  broker_party_id=brokers["b"], name=f"OUS b {sfx}",
                                  lifecycle="active"))
        contract_other = add(Contract(tenant_id=carrier.id, program_id=other_prog.id,
                                      broker_party_id=brokers["a"], name=f"OUS o {sfx}",
                                      lifecycle="active"))
        admin = add(AppUser(tenant_id=carrier.id, email=f"ous-a-{sfx}@carrier.test",
                            full_name="Carrier Admin", role="carrier_admin",
                            invited_by_user_id=founder.id))
        mga = carrier.tenant_name
        ids = {"tid": carrier.id, "cpid": cpid, "prog": prog.id, "mga": mga,
               "broker_a": brokers["a"], "broker_b": brokers["b"], "broker_off": brokers["off"],
               "contract_b": contract_b.id, "contract_other": contract_other.id,
               "h": {"Authorization": f"Bearer {mint_access_token(admin.id, carrier.id, 'carrier_admin')}"}}
    yield ids
    with SessionLocal() as s:                      # by id only — never a cascade
        # Rows the uploads made: the layouts and their Audit Logs entries. All
        # belong to this test's own fresh carrier, so its id is the whole filter.
        for model in (ExportTemplate, ActivityEvent):
            for (pk,) in s.query(model.id).filter(model.tenant_id == ids["tid"]).all():
                made.append((model, pk))
        for model, pk in reversed(made):
            try:
                obj = s.get(model, pk)
                if obj is not None:
                    s.delete(obj); s.commit()
            except Exception:  # noqa: BLE001 — cleanup must not mask a result
                s.rollback()


def _upload(w, name, **scope):
    data = {"mga": w["mga"], "name": name, "carrier_party_id": str(w["cpid"]),
            **{k: str(v) for k, v in scope.items()}}
    return client.post("/export/template/generate", headers=w["h"], data=data,
                       files={"file": ("sample.xlsx", _sample(),
                                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})


def _row(tid):
    with SessionLocal() as s:
        t = s.get(ExportTemplate, tid)
        return (t.program_id, t.broker_party_id, t.contract_id, t.source_kind,
                t.version, t.is_active)


def _count(w):
    with SessionLocal() as s:
        return s.query(ExportTemplate).filter(ExportTemplate.tenant_id == w["tid"]).count()


# ══════════════════════════════════════════════════════════════════════════
def test_an_upload_is_filed_under_its_programme_and_broker(w, ai_calls):
    r = _upload(w, "OUS — broker a", program_id=w["prog"], broker_party_id=w["broker_a"])
    assert r.status_code == 200, r.text
    assert _row(r.json()["id"]) == (w["prog"], w["broker_a"], None, "uploaded", 1, 1)


def test_an_upload_for_a_contract_on_file_is_filed_under_it_too(w, ai_calls):
    r = _upload(w, "OUS — broker b — contract", program_id=w["prog"],
                broker_party_id=w["broker_b"], contract_id=w["contract_b"])
    assert r.status_code == 200, r.text
    assert _row(r.json()["id"]) == (w["prog"], w["broker_b"], w["contract_b"], "uploaded", 1, 1)


@pytest.mark.parametrize("scope,words", [
    ({"broker_party_id": "broker_off"}, "not on this programme"),
    ({"broker_party_id": "broker_a", "contract_id": "contract_b"}, "different broker"),
    ({"broker_party_id": "broker_a", "contract_id": "contract_other"}, "not on this programme"),
])
def test_a_scope_the_hierarchy_does_not_hold_is_refused_first(w, ai_calls, scope, words):
    before = _count(w)
    r = _upload(w, "OUS refused", program_id=w["prog"],
                **{k: w[v] for k, v in scope.items()})
    assert r.status_code == 400 and words in r.text, r.text
    assert _count(w) == before and ai_calls == []


@pytest.mark.parametrize("scope", [("broker_party_id", "broker_a"),
                                   ("contract_id", "contract_b")])
def test_a_broker_or_contract_needs_its_programme(w, ai_calls, scope):
    before = _count(w)
    r = _upload(w, "OUS refused", **{scope[0]: w[scope[1]]})
    assert r.status_code == 400 and "together with its programme" in r.text, r.text
    assert _count(w) == before and ai_calls == []


def test_each_broker_finds_its_own_uploaded_layout_never_the_others(w, ai_calls):
    a = _upload(w, "OUS — broker a", program_id=w["prog"], broker_party_id=w["broker_a"]).json()["id"]
    b = _upload(w, "OUS — broker b", program_id=w["prog"], broker_party_id=w["broker_b"]).json()["id"]
    with SessionLocal() as s:
        for broker, mine in ((w["broker_a"], a), (w["broker_b"], b)):
            t, level = _resolve(s, w["tid"], w["cpid"], w["prog"], broker, None)
            assert (t.id, level) == (mine, "broker")
    # Separate names keep them separate templates: each is version 1, both live.
    assert _row(a)[4:] == (1, 1) and _row(b)[4:] == (1, 1)


def test_the_old_call_with_no_programme_still_saves(w, ai_calls):
    r = _upload(w, "OUS — legacy")
    assert r.status_code == 200, r.text
    assert _row(r.json()["id"]) == (None, None, None, "uploaded", 1, 1)
