"""
test_contract_wording_topup_flow.py — the same failure, through the endpoints.

test_contract_wording_topup.py holds the rule as arithmetic. This one walks the
journey the carrier actually made when they reported it:

    fill in the basics → look at the wording (two sections) → go back and agree
    the terms → return to the wording → Read It Through → save → the PDF the
    signature screen shows

and asserts the thing that was wrong: the clauses, the saved contract and the
composed document all state the terms that were agreed LAST, not the ones that
happened to be typed before the wording was first opened.

Needs the database, like every other flow test here. It adds its own throwaway
carrier and broker and changes nothing that was already there.
"""
import os

os.environ["MAIL_ALLOWED_RECIPIENTS"] = "nobody@example.invalid"
os.environ.setdefault("APP_BASE_URL", "http://localhost:5173")

import fitz
import pytest
from fastapi.testclient import TestClient

import main
from auth_tokens import mint_access_token
from db import (
    AppUser, Party, Program, ProgramBroker, SessionLocal, Tenant,
)

client = TestClient(main.app)

BASICS = {
    "name": "Spectrum Transportation binder",
    "class_of_business": "Commercial Motor",
    "inception_dt": "2027-01-01",
    "expiry_dt": "2027-12-31",
}


@pytest.fixture(scope="module")
def world():
    """A carrier, a broker on one of its programmes, and an admin each."""
    sfx = os.urandom(4).hex()
    with SessionLocal() as s:
        carrier = Tenant(tenant_name=f"topup-{sfx}", legal_name="Insurisk Specialty")
        s.add(carrier); s.commit()
        broker = Party(tenant_id=carrier.id, party_type="broker",
                       legal_name="CRC Insurisk", reference=f"topup-b-{sfx}")
        s.add(broker); s.commit()
        prog = Program(tenant_id=carrier.id, name="Spectrum Transportation",
                       is_app_managed=True)
        s.add(prog); s.commit()
        s.add(ProgramBroker(tenant_id=carrier.id, program_id=prog.id,
                            broker_party_id=broker.id, status="active"))
        founder = (s.query(AppUser).filter(AppUser.role == "kavachio_admin")
                   .order_by(AppUser.id).first())
        if founder is None:
            pytest.skip("no kavachio_admin on this database to seed an invite chain")
        dana = AppUser(tenant_id=carrier.id, email=f"dana-{sfx}@insurisk.test",
                       full_name="Dana Alvarez", role="carrier_admin",
                       invited_by_user_id=founder.id)
        s.add(dana); s.commit()
        return {
            "tenant": carrier.id, "broker": broker.id, "program": prog.id,
            "carrier": {"Authorization":
                        f"Bearer {mint_access_token(dana.id, carrier.id, 'carrier_admin')}"},
        }


def _post(path, headers, body):
    r = client.post(path, headers=headers, json=body)
    assert r.status_code == 200, f"{path} → {r.status_code} {r.text}"
    return r.json()


def _preview(headers, *, limits, sections=None, values=None, **extra):
    return _post("/contract-wording/preview", headers, {
        "contract_type": "insurer_broker",
        "values": values or BASICS,
        "agreed_limits": limits,
        "sections": sections,
        "carrier_name": "Insurisk Specialty",
        "counterparty_name": "CRC Insurisk",
        **extra,
    })


def _pdf_text(cid, headers):
    r = client.get(f"/contracts/{cid}/contract.pdf", headers=headers)
    assert r.status_code == 200, r.text
    doc = fitz.open(stream=r.content, filetype="pdf")
    return " ".join("".join(p.get_text() for p in doc).split())


def _sec(sections, key):
    return next(s for s in sections if s["key"] == key)


def test_the_wizard_writes_the_clause_when_the_carrier_comes_back_with_terms(world):
    carrier = world["carrier"]

    # Step 1 with the basics only, then straight to the wording: two sections.
    first = _preview(carrier, limits={})
    assert [s["key"] for s in first["sections"]] == ["parties", "reporting"]
    assert first["added"] == []

    # Back to Terms, a commission agreed, forward to the wording again — which
    # is exactly what go(1) does: it sends the sections it already has.
    second = _preview(carrier, limits={"commission_max_pct": {"value": "12.5"}},
                      sections=first["sections"])

    assert [a["key"] for a in second["added"]] == ["commission_max_pct"]
    fin = _sec(second["sections"], "financial")
    assert "{{commission_max_pct}}" in fin["body"]
    assert "12.5%" in fin["rendered"]
    # And Read It Through stops warning about a term stated nowhere.
    assert not [w for w in second["warnings"] if "does not quote" in w["title"]]


def test_a_clause_deleted_in_the_wizard_is_not_written_back(world):
    carrier = world["carrier"]
    limits = {"commission_max_pct": {"value": "12.5"}}
    topped = _preview(carrier, limits=limits,
                      sections=_preview(carrier, limits={})["sections"])

    kept = [s for s in topped["sections"] if s["key"] != "financial"]
    again = _preview(carrier, limits=limits, sections=kept,
                     dropped_sections=["financial"],
                     dropped_terms=["commission_max_pct"])

    assert again["added"] == []
    assert "financial" not in [s["key"] for s in again["sections"]]


def test_the_saved_contract_and_its_pdf_state_the_late_term(world):
    """The report, end to end: the sections the wizard holds are the stale two,
    the terms carry a commission, and the document has to say so."""
    carrier = world["carrier"]
    stale = _preview(carrier, limits={})["sections"]

    rec = _post("/contracts", carrier, {
        "program_id": world["program"], "contract_type": "insurer_broker",
        "counterparty_party_id": world["broker"],
        **BASICS,
        "agreed_limits": {"commission_max_pct": {"value": "12.5"}},
        "wording_sections": [{k: s[k] for k in ("key", "title", "body", "origin")}
                             for s in stale],
    })

    fin = _sec(rec["wording_sections"], "financial")
    assert "{{commission_max_pct}}" in fin["body"]
    assert "12.5%" in fin["rendered"]
    # The term is stated, so nothing is left checked-but-unsaid.
    assert not rec["wording_unquoted"]
    # And the file the signature screen shows is composed from those sections.
    assert "commission not exceeding 12.5%" in _pdf_text(rec["id"], carrier)


def test_a_term_agreed_on_the_record_gets_its_clause_too(world):
    """The same hole on the other screen: the terms panel saves limits without
    the wording, so a term added there had no sentence either."""
    carrier = world["carrier"]
    stale = _preview(carrier, limits={})["sections"]
    rec = _post("/contracts", carrier, {
        "program_id": world["program"], "contract_type": "insurer_broker",
        "counterparty_party_id": world["broker"],
        **BASICS, "name": "Spectrum Transportation binder — record",
        "agreed_limits": {"commission_max_pct": {"value": "12.5"}},
        "wording_sections": [{k: s[k] for k in ("key", "title", "body", "origin")}
                             for s in stale],
    })
    cid = rec["id"]

    r = client.patch(f"/contracts/{cid}", headers=carrier, json={
        "agreed_limits": {"commission_max_pct": {"value": "12.5"},
                          "max_sum_insured": {"value": "500000"},
                          "currency": {"value": "GBP"}}})
    assert r.status_code == 200, r.text
    saved = r.json()

    assert saved["wording_added"], "the new terms had no clause quoting them"
    auth = _sec(saved["wording_sections"], "authority")
    assert "{{max_sum_insured}}" in auth["body"]
    assert not saved["wording_unquoted"]
    text = _pdf_text(cid, carrier)
    assert "sum insured on any one risk shall not exceed GBP 500,000" in text


def test_nothing_is_added_when_the_wording_already_says_it(world):
    """The ordinary case, which is every other visit to these screens: the
    document already states its terms and must come back untouched."""
    carrier = world["carrier"]
    limits = {"commission_max_pct": {"value": "12.5"}}
    once = _preview(carrier, limits=limits,
                    sections=_preview(carrier, limits={})["sections"])
    twice = _preview(carrier, limits=limits, sections=once["sections"])

    assert twice["added"] == []
    assert [s["body"] for s in twice["sections"]] == \
        [s["body"] for s in once["sections"]]


def test_a_clause_deleted_on_the_record_stays_deleted_when_it_reopens(world):
    """The other half of the rule. The wording editor re-reads through the
    preview every time it opens, so without a memory of the deletion the clause
    would be written straight back and deleting it would be impossible."""
    carrier = world["carrier"]
    stale = _preview(carrier, limits={})["sections"]
    rec = _post("/contracts", carrier, {
        "program_id": world["program"], "contract_type": "insurer_broker",
        "counterparty_party_id": world["broker"],
        **BASICS, "name": "Spectrum Transportation binder — deleted clause",
        "agreed_limits": {"commission_max_pct": {"value": "12.5"}},
        "wording_sections": [{k: s[k] for k in ("key", "title", "body", "origin")}
                             for s in stale],
    })
    cid = rec["id"]
    assert "financial" in [s["key"] for s in rec["wording_sections"]]

    # The carrier deletes that clause and saves.
    kept = [{k: s[k] for k in ("key", "title", "body", "origin")}
            for s in rec["wording_sections"] if s["key"] != "financial"]
    r = client.patch(f"/contracts/{cid}", headers=carrier,
                     json={"wording_sections": kept})
    assert r.status_code == 200, r.text
    saved = r.json()

    assert "financial" not in [s["key"] for s in saved["wording_sections"]]
    # Still reported, which is the right answer for a term checked on every row
    # and stated nowhere — a warning, not a clause put back.
    assert [u["key"] for u in saved["wording_unquoted"]] == ["commission_max_pct"]
    assert "commission_max_pct" in saved["wording_dropped"]["terms"]

    # Reopening the editor: the same preview call the record screen makes.
    again = _preview(
        carrier, limits={"commission_max_pct": {"value": "12.5"}},
        sections=[{k: s[k] for k in ("key", "title", "body", "origin")}
                  for s in saved["wording_sections"]],
        dropped_sections=saved["wording_dropped"]["sections"],
        dropped_terms=saved["wording_dropped"]["terms"])

    assert again["added"] == []
    assert "financial" not in [s["key"] for s in again["sections"]]


def test_a_term_written_back_in_starts_being_topped_up_again(world):
    """Self-healing: a deletion is remembered until the wording states the term
    again, and then it is an ordinary term like any other. Otherwise one
    deletion would silence the top-up for that term for the life of the
    contract."""
    carrier = world["carrier"]
    stale = _preview(carrier, limits={})["sections"]
    rec = _post("/contracts", carrier, {
        "program_id": world["program"], "contract_type": "insurer_broker",
        "counterparty_party_id": world["broker"],
        **BASICS, "name": "Spectrum Transportation binder — written back",
        "agreed_limits": {"commission_max_pct": {"value": "12.5"}},
        "wording_sections": [{k: s[k] for k in ("key", "title", "body", "origin")}
                             for s in stale],
    })
    cid = rec["id"]
    kept = [{k: s[k] for k in ("key", "title", "body", "origin")}
            for s in rec["wording_sections"] if s["key"] != "financial"]
    r = client.patch(f"/contracts/{cid}", headers=carrier,
                     json={"wording_sections": kept})
    assert "commission_max_pct" in r.json()["wording_dropped"]["terms"]

    # The carrier writes the clause again themselves, quoting the term.
    back = kept + [{"key": "financial", "title": "Financial terms",
                    "origin": "your own words",
                    "body": "Commission is capped at {{commission_max_pct}}."}]
    r = client.patch(f"/contracts/{cid}", headers=carrier,
                     json={"wording_sections": back})
    assert r.status_code == 200, r.text
    saved = r.json()

    assert saved["wording_dropped"]["terms"] == []
    assert not saved["wording_unquoted"]


def test_saving_a_term_by_itself_does_not_write_back_a_deleted_clause(world):
    """The terms panel saves limits with no wording attached, and that path
    tops the wording up as well. It has to honour the same deletions, or the
    clause somebody removed comes back the moment a figure is corrected."""
    carrier = world["carrier"]
    stale = _preview(carrier, limits={})["sections"]
    rec = _post("/contracts", carrier, {
        "program_id": world["program"], "contract_type": "insurer_broker",
        "counterparty_party_id": world["broker"],
        **BASICS, "name": "Spectrum Transportation binder — terms only",
        "agreed_limits": {"commission_max_pct": {"value": "12.5"}},
        "wording_sections": [{k: s[k] for k in ("key", "title", "body", "origin")}
                             for s in stale],
    })
    cid = rec["id"]
    kept = [{k: s[k] for k in ("key", "title", "body", "origin")}
            for s in rec["wording_sections"] if s["key"] != "financial"]
    r = client.patch(f"/contracts/{cid}", headers=carrier,
                     json={"wording_sections": kept})
    assert "commission_max_pct" in r.json()["wording_dropped"]["terms"]

    # The carrier corrects the commission on the terms panel — wording untouched.
    r = client.patch(f"/contracts/{cid}", headers=carrier,
                     json={"agreed_limits": {"commission_max_pct": {"value": "13"}}})
    assert r.status_code == 200, r.text
    saved = r.json()

    assert saved["wording_added"] == []
    assert "financial" not in [s["key"] for s in saved["wording_sections"]]
    # And it is still reported as a term the document does not state, which is
    # the honest answer for a contract somebody chose not to have a clause in.
    assert [u["key"] for u in saved["wording_unquoted"]] == ["commission_max_pct"]
