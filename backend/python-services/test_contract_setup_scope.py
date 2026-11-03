"""One broker, two contracts, two BDX templates — and a setup for each.

THE GAP. A setup was chosen per (carrier, programme, broker), and making one
live switched off every other live setup for that broker. So a broker holding
two contracts could only ever have ONE layout live, and a contract with no
template of its own was quietly handed the template — and the setup — built
for a different contract: Process Bordereau showed contract B the layout agreed
for contract A, downloaded A's blank bordereau for it, and ran B's file on A's
setup.

What must now hold:
  · a contract resolves ITS template, never another contract's
  · activating B's setup leaves A's live; a setup covering the same contract
    (or a legacy one covering all) still replaces it
  · the run, the broker's readiness, the resolve endpoint and the template
    download all name the setup for the contract picked
  · with no contract named, nothing changes

    python -m pytest test_contract_setup_scope.py
"""
from __future__ import annotations

import os

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import main  # noqa: F401 — loads .env, which db needs at import
import direct_routes as dr
from auth_tokens import mint_access_token
from db import (
    AppUser, Contract, DirectFormat, ExportTemplate, LandingRecord, Party,
    Pipeline, PipelineContract, Program, ProgramBroker, SessionLocal, Tenant,
)
from fingerprint import signature_hash
from mapper import read_excel_all_sheets, signature_multi
from output_template_routes import _resolve
from setup_scope import live_setup_for

client = TestClient(main.app)

LAYOUT_A = {"Risk BDX": ["UMR", "Premium"]}
LAYOUT_B = {"Lloyds": ["Certificate Ref", "Gross Written Premium", "Country"]}


def _fp(sheets):
    return signature_hash(signature_multi(
        {n: pd.DataFrame([["x"] * len(c)], columns=c) for n, c in sheets.items()}))


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

        carrier = add(Tenant(tenant_name=f"css-{sfx}", legal_name="Scope Carrier"))
        cpid = _ensure_carrier_party(s, carrier.id); s.commit()
        made.append((Party, cpid))
        broker = add(Party(tenant_id=carrier.id, party_type="broker",
                           legal_name="Scope Broker", reference=f"css-b-{sfx}"))
        prog = add(Program(tenant_id=carrier.id, name=f"CSS {sfx}", is_app_managed=True))
        add(ProgramBroker(tenant_id=carrier.id, program_id=prog.id,
                          broker_party_id=broker.id, status="active"))
        seat = add(AppUser(tenant_id=None, broker_party_id=broker.id,
                           email=f"css-b-{sfx}@broker.test", full_name="Broker Seat",
                           role="broker_admin", invited_by_user_id=founder.id))
        admin = add(AppUser(tenant_id=carrier.id, email=f"css-a-{sfx}@carrier.test",
                            full_name="Carrier Admin", role="carrier_admin",
                            invited_by_user_id=founder.id))

        ids = {"tid": carrier.id, "cpid": cpid, "prog": prog.id, "broker": broker.id}
        for key, layout, sheet in (("a", LAYOUT_A, "Risk BDX"), ("b", LAYOUT_B, "Lloyds")):
            c = add(Contract(tenant_id=carrier.id, program_id=prog.id,
                             broker_party_id=broker.id, name=f"CSS {key} {sfx}",
                             lifecycle="active"))
            t = add(ExportTemplate(tenant_id=carrier.id, name=f"CSS template {key} {sfx}",
                                   version=1, is_active=1, carrier_party_id=cpid,
                                   program_id=prog.id, broker_party_id=broker.id,
                                   contract_id=c.id, structure={"sheets": [{
                                       "sheet_name": "Out", "header_row": 0, "data_start_row": 1,
                                       "columns": [{"column_index": 0, "column_name": key.upper()}]}]}))
            f = add(DirectFormat(tenant_id=carrier.id, name=f"CSS format {key} {sfx}",
                                 fingerprint=_fp(layout), output_template_id=t.id,
                                 contract_id=c.id, carrier_party_id=cpid, program_id=prog.id,
                                 approved=0, sheet_routing={"routes": [{
                                     "output_sheet": "Out", "sources": [{"input_sheet": sheet}]}]}))
            add(LandingRecord(tenant_id=carrier.id, format_id=f.id, fingerprint=f.fingerprint,
                              source_filename=f"css-{key}.xlsx", row_count=0,
                              data={"sheets": {n: {"columns": cols, "rows": []}
                                               for n, cols in layout.items()}, "row_count": 0}))
            ids.update({f"contract_{key}": c.id, f"tpl_{key}": t.id, f"fmt_{key}": f.id})
        c = add(Contract(tenant_id=carrier.id, program_id=prog.id, broker_party_id=broker.id,
                         name=f"CSS c {sfx}", lifecycle="active"))
        ids["contract_c"] = c.id

        def setup(key, contract_ids, status):
            p = add(Pipeline(tenant_id=carrier.id, name=f"CSS setup {key} {sfx}",
                             carrier_party_id=cpid, program_id=prog.id,
                             broker_party_id=broker.id,
                             input_format_id=ids[f"fmt_{key[0]}"],
                             output_template_id=ids[f"tpl_{key[0]}"], status=status))
            for cid in contract_ids:
                add(PipelineContract(tenant_id=carrier.id, pipeline_id=p.id, contract_id=cid))
            return p.id

        ids["setup"] = setup
        ids["pipe_a"] = setup("a", [ids["contract_a"]], "active")
        ids["pipe_b"] = setup("b", [ids["contract_b"]], "draft")
        ids["chain"] = (f"/carriers/{carrier.id}/programs/{prog.id}/brokers/{broker.id}"
                        f"/contracts/{{}}")
        ids["broker_h"] = {"Authorization": f"Bearer {mint_access_token(seat.id, None, 'broker_admin')}"}
        ids["carrier_h"] = {"Authorization": f"Bearer {mint_access_token(admin.id, carrier.id, 'carrier_admin')}"}
    yield ids
    with SessionLocal() as s:                      # by id only — never a cascade
        for model, pk in reversed(made):
            try:
                obj = s.get(model, pk)
                if obj is not None:
                    s.delete(obj); s.commit()
            except Exception:  # noqa: BLE001 — cleanup must not mask a result
                s.rollback()


def _activate(pipeline_id):
    with SessionLocal() as s:
        dr._activate_pipeline(s, s.get(Pipeline, pipeline_id))
        s.commit()


def _status(pipeline_id):
    with SessionLocal() as s:
        return s.get(Pipeline, pipeline_id).status


# ══════════════════════════════════════════════════════════════════════════
def test_a_contract_gets_its_own_template_and_never_another_contracts(w):
    _activate(w["pipe_b"])
    with SessionLocal() as s:
        args = (s, w["tid"], w["cpid"], w["prog"], w["broker"])
        assert _resolve(*args, w["contract_a"])[0].id == w["tpl_a"]
        assert _resolve(*args, w["contract_b"])[0].id == w["tpl_b"]
        # C has no template. Two setups are live, each built for somebody else's
        # contract — and neither of their templates is C's to use.
        assert _resolve(*args, w["contract_c"]) == (None, None)


def test_a_setup_that_lists_the_contract_lends_it_its_template(w):
    """Deliberately built to cover C as well — that is the carrier's answer."""
    with SessionLocal() as s:
        pc = PipelineContract(tenant_id=w["tid"], pipeline_id=w["pipe_a"],
                              contract_id=w["contract_c"])
        s.add(pc); s.commit()
        try:
            t, level = _resolve(s, w["tid"], w["cpid"], w["prog"], w["broker"], w["contract_c"])
            assert t.id == w["tpl_a"] and level == "programme"
            assert live_setup_for(s, w["tid"], w["cpid"], w["prog"], w["broker"],
                                  w["contract_c"]).id == w["pipe_a"]
        finally:
            s.delete(pc); s.commit()


def test_activating_one_contracts_setup_leaves_the_others_live(w):
    _activate(w["pipe_a"])            # made live the real way, so its format is approved
    _activate(w["pipe_b"])
    assert _status(w["pipe_a"]) == "active" and _status(w["pipe_b"]) == "active"
    with SessionLocal() as s:
        assert s.get(DirectFormat, w["fmt_a"]).approved == 1   # A's format not withdrawn
        assert s.get(DirectFormat, w["fmt_b"]).approved == 1


def test_a_setup_for_the_same_contract_still_replaces_it(w):
    _activate(w["pipe_b"])
    again = w["setup"]("a2", [w["contract_a"]], "draft")
    _activate(again)
    assert _status(w["pipe_a"]) == "superseded"
    assert _status(again) == "active" and _status(w["pipe_b"]) == "active"


def test_a_legacy_setup_listing_no_contracts_is_still_replaced(w):
    """It covered every contract, so anything made live after it replaces it —
    exactly as every activation did before contracts were considered."""
    legacy = w["setup"]("a-legacy", [], "active")
    _activate(w["pipe_b"])
    assert _status(legacy) == "superseded"


def test_the_setup_that_runs_follows_the_contract(w):
    _activate(w["pipe_b"])
    with SessionLocal() as s:
        pick = lambda cid: live_setup_for(s, w["tid"], w["cpid"], w["prog"], w["broker"], cid)
        assert pick(w["contract_a"]).id == w["pipe_a"]
        assert pick(w["contract_b"]).id == w["pipe_b"]
        assert pick(None).id == w["pipe_b"]          # no contract: the newest, as before


def test_every_screen_names_the_setup_for_the_contract_picked(w):
    _activate(w["pipe_b"])
    for key, layout in (("a", LAYOUT_A), ("b", LAYOUT_B)):
        cid, pipe = w[f"contract_{key}"], w[f"pipe_{key}"]
        r = client.get(w["chain"].format(cid) + "/bordereau", headers=w["broker_h"])
        assert r.status_code == 200 and r.json()["setup"]["id"] == pipe, r.text
        r = client.get(w["chain"].format(cid) + "/bordereau-template", headers=w["broker_h"])
        assert r.status_code == 200, r.text
        got = {n: [str(c) for c in df.columns]
               for n, df in read_excel_all_sheets(r.content, 0).items()}
        assert got == layout
        r = client.get("/output-template/resolve", headers=w["carrier_h"], params={
            "program_id": w["prog"], "carrier_party_id": w["cpid"],
            "broker_party_id": w["broker"], "contract_id": cid})
        body = r.json()
        assert r.status_code == 200 and body["template"]["id"] == w[f"tpl_{key}"], r.text
        assert body["setup"]["pipeline_id"] == pipe and body["setup"]["matches"] is True
    r = client.get("/output-template/resolve", headers=w["carrier_h"], params={
        "program_id": w["prog"], "carrier_party_id": w["cpid"],
        "broker_party_id": w["broker"], "contract_id": w["contract_c"]})
    assert r.json()["found"] is False
