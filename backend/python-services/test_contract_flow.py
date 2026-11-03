"""
The whole of Create-a-Contract, once, in order.

Every other test file here takes one step and pushes on it. This one does the
opposite: it walks the journey a carrier and a broker actually make, end to
end, and asserts only the things that are true BETWEEN the steps — because the
failures this catches are not failures of a step. They are two steps that each
work and disagree about what happened in the one before.

    write it → send for review → broker pushes back → carrier applies and
    re-sends → broker agrees → carrier signs → broker signs → in force

The three joins that used to be broken, and are what this exists to hold:

  * a change request naming a LIMIT (which is where the money is) can be made,
    applied, and lands in `agreed_limits` rather than as a column nobody has;
  * a signature written in the round reaches `contract_signature`, so whose
    turn it is moves DURING the round and not only at the end of it;
  * the terms both sides agreed are the terms in the PDF they sign, because
    the round composes it from the same sections the download does.
"""
import os

os.environ["MAIL_ALLOWED_RECIPIENTS"] = "nobody@example.invalid"
os.environ.setdefault("APP_BASE_URL", "http://localhost:5173")

import pytest
from fastapi.testclient import TestClient

import main
from auth_tokens import mint_access_token
from db import (
    AppUser, Contract, Party, Program, ProgramBroker, SessionLocal, Tenant,
)

client = TestClient(main.app)

TINY_PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
            "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


@pytest.fixture(scope="module")
def world():
    """A carrier, a broker on one of its programmes, and an admin each."""
    sfx = os.urandom(4).hex()
    with SessionLocal() as s:
        carrier = Tenant(tenant_name=f"flow-{sfx}", legal_name="Insurisk Specialty")
        s.add(carrier); s.commit()
        broker = Party(tenant_id=carrier.id, party_type="broker",
                       legal_name="CRC Insurisk", reference=f"flow-b-{sfx}")
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
        marco = AppUser(tenant_id=None, broker_party_id=broker.id,
                        email=f"marco-{sfx}@crc.test", full_name="Marco Vance",
                        role="broker_admin", invited_by_user_id=dana.id)
        s.add(marco); s.commit()
        return {
            "tenant": carrier.id, "broker": broker.id, "program": prog.id,
            # The signers named in the wizard are the REAL seats. A carrier who
            # types an address the broker does not log in with gets a different
            # and also-correct behaviour — the round re-points at whoever
            # actually turns up and retires the link sent to the other address
            # (test_esign covers both). Naming the real one here keeps this
            # test about the journey rather than about that fork.
            "dana_email": dana.email, "marco_email": marco.email,
            "carrier": {"Authorization":
                        f"Bearer {mint_access_token(dana.id, carrier.id, 'carrier_admin')}"},
            "brokerh": {"Authorization":
                        f"Bearer {mint_access_token(marco.id, None, 'broker_admin')}"},
        }


def _post(path, headers, body=None):
    r = client.post(path, headers=headers, json=body if body is not None else {})
    assert r.status_code == 200, f"{path} → {r.status_code} {r.text}"
    return r.json()


def _get(path, headers):
    r = client.get(path, headers=headers)
    assert r.status_code == 200, f"{path} → {r.status_code} {r.text}"
    return r.json()


def test_the_whole_journey(world):
    carrier, brokerh = world["carrier"], world["brokerh"]

    # ── 1. write it ────────────────────────────────────────────────────────
    # As the wizard leaves it: terms, wording as sections, signatories named.
    rec = _post("/contracts", carrier, {
        "program_id": world["program"], "contract_type": "insurer_broker",
        "name": "Schedule A — 2027", "counterparty_party_id": world["broker"],
        "class_of_business": "Commercial Property",
        "inception_dt": "2027-01-01", "expiry_dt": "2027-12-31",
        "agreed_limits": {"commission_pct": {"value": "25"}},
        "wording_sections": [
            {"key": "parties", "title": "Parties and cover", "origin": "generated",
             "body": "This Agreement is made between {{carrier_name}} (the "
                     "“Carrier”) and {{counterparty_name}} (the "
                     "“Broker”)."},
            {"key": "financial", "title": "Financial terms", "origin": "generated",
             "body": "Commission is payable at {{commission_pct}}."},
        ],
        "signers": [
            {"name": "Dana Alvarez", "email": world["dana_email"],
             "role": "Carrier Admin", "side": "carrier"},
            {"name": "Marco Vance", "email": world["marco_email"],
             "role": "Broker Admin", "side": "counterparty"},
        ],
    })
    cid = rec["id"]
    assert rec["lifecycle"] == "draft"

    # Nothing to sign yet — the terms have not been anywhere near the broker.
    r = client.post(f"/esign/contracts/{cid}/signing-session", headers=carrier)
    assert r.status_code == 409
    assert "terms are not settled" in r.json()["detail"]

    # ── 2. send the terms out ──────────────────────────────────────────────
    rec = _post(f"/contracts/{cid}/send-for-review", carrier, {})
    assert rec["lifecycle"] == "in_review"
    assert rec["whose_turn"] == "broker"

    # ── 3. the broker pushes back — on the COMMISSION, not the name ────────
    rec = _post(f"/contracts/{cid}/request-changes", brokerh, {
        "note": "", "changes": [{"field": "commission_pct", "current": "25",
                                 "proposed": "26",
                                 "comment": "in line with last year"}]})
    assert rec["lifecycle"] == "changes_requested"
    assert rec["whose_turn"] == "carrier"
    asked = rec["open_change_request"]["proposed_changes"][0]
    assert asked["field"] == "commission_pct" and asked["proposed"] == "26"

    # ── 4. the carrier applies it, exactly as the button does ──────────────
    limits = dict(rec["agreed_limits"] or {})
    limits["commission_pct"] = {**limits.get("commission_pct", {}), "value": "26"}
    r = client.patch(f"/contracts/{cid}", headers=carrier,
                     json={"agreed_limits": limits})
    assert r.status_code == 200, r.text
    # Stored as a number, not the string that was typed — clean_agreed_limits
    # normalises a percent on the way in.
    assert float(r.json()["agreed_limits"]["commission_pct"]["value"]) == 26.0

    # AND THE CONTRACT NOW SAYS SO. The clause quotes the term rather than the
    # number, so the sentence people read has to move with it — a record that
    # still reads "25%" beside a check enforcing 26% is the same contract
    # saying two things.
    fin = next(sec for sec in r.json()["wording_sections"]
               if sec["key"] == "financial")
    assert "26%" in fin["rendered"], fin["rendered"]
    assert "25%" not in fin["rendered"]
    assert "{{commission_pct}}" in fin["body"], "the token is what makes it move"
    assert not r.json()["wording_unquoted"], "the clause still quotes the term"

    rec = _post(f"/contracts/{cid}/send-for-review", carrier, {})
    assert rec["lifecycle"] == "in_review"
    # Answered, so it is off the top of the carrier's screen.
    assert rec["open_change_request"] is None

    # ── 5. the broker agrees ───────────────────────────────────────────────
    rec = _post(f"/contracts/{cid}/accept-terms", brokerh, {})
    assert rec["lifecycle"] == "agreed"
    # THE CARRIER SIGNS FIRST. Not the broker, whatever the state name says.
    assert rec["whose_turn"] == "carrier"
    # And the carrier is not offered a review of terms the broker has just
    # agreed. There is nothing left to review, the next thing that happens is a
    # signature, and the transition is refused anyway — a button that 409s is
    # worse than no button.
    assert rec["actions"]["send_for_review"] is False
    r = client.post(f"/contracts/{cid}/send-for-review", headers=carrier, json={})
    assert r.status_code == 409, r.text

    # The broker cannot open a round the carrier has not started.
    r = client.post(f"/esign/contracts/{cid}/signing-session", headers=brokerh)
    assert r.status_code == 409
    assert "not sent this for signature" in r.json()["detail"]

    # ── 6. the carrier signs, in the app, with no email and no code ────────
    mine = _post(f"/esign/contracts/{cid}/signing-session", carrier)
    view = _get(f"/esign/sign/{mine['token']}",
                {"X-Esign-Session": mine["session"]})
    assert view["locked"] is False

    # The document they are signing is the wording that was AGREED — 26%,
    # the number the broker asked for and the carrier applied.
    pdf = client.get(f"/esign/sign/{mine['token']}/pdf",
                     headers={"X-Esign-Session": mine["session"]})
    assert pdf.status_code == 200
    import fitz
    with fitz.open(stream=pdf.content, filetype="pdf") as doc:
        text = "".join(pg.get_text() for pg in doc)
    assert "26" in text, "the signed document must carry the agreed commission"
    # The signing ANCHORS are meant to be in there — invisibly — and are how
    # the boxes are found. A wording token is not: a clause that cannot state
    # its own number has no business on a page somebody is about to sign.
    import re
    left = re.sub(r"\{\{(?:signature|initial|name|title|date|text):"
                  r"(?:tenant|broker):\d+\}\}", "", text)
    assert "{{" not in left, "no unresolved wording token may reach a signable document"

    done = _post(f"/esign/sign/{mine['token']}",
                 {"X-Esign-Session": mine["session"]},
                 {"agreed": True, "signature_name": "Dana Alvarez",
                  "signature_image": TINY_PNG,
                  "fields": [{"field_id": f["id"],
                              "value": "Dana Alvarez" if f["type"] in
                                       ("signature", "initial", "name")
                                       else "Carrier Admin" if f["type"] == "title"
                                       else "07 Sep 2026"}
                             for f in view["fields"] if f["mine"]]})
    assert done["status"] == "in_progress"

    # ── 7. it is now the broker's, and they were told ──────────────────────
    rec = _get(f"/contracts/{cid}", brokerh)
    assert rec["whose_turn"] == "broker", (
        "the carrier's signature must move the contract into the broker's "
        "queue DURING the round, not at the end of it")
    assert [g["side"] for g in rec["signatures"]] == ["carrier"]
    assert rec["lifecycle"] == "agreed"

    dash = _get("/broker/dashboard", brokerh)
    assert any(w["id"] == cid for w in dash["waiting_on_me"]), (
        "and it must show on the dashboard they actually look at")

    # Their link went out with the handover email.
    env = _get(f"/esign/envelopes/{mine['envelope_id']}", carrier)
    theirs = next(r for r in env["recipients"] if r["side"] == "broker")
    assert theirs["status"] == "sent" and theirs["link"]

    # ── 8. the broker signs, from their own seat ───────────────────────────
    hers = _post(f"/esign/contracts/{cid}/signing-session", brokerh)
    assert hers["token"] == theirs["link"].split("token=")[-1], (
        "opening in the app must not retire the link already in their inbox")
    view = _get(f"/esign/sign/{hers['token']}",
                {"X-Esign-Session": hers["session"]})
    done = _post(f"/esign/sign/{hers['token']}",
                 {"X-Esign-Session": hers["session"]},
                 {"agreed": True, "signature_name": "Marco Vance",
                  "signature_image": TINY_PNG,
                  "fields": [{"field_id": f["id"],
                              "value": "Marco Vance" if f["type"] in
                                       ("signature", "initial", "name")
                                       else "Broker Admin" if f["type"] == "title"
                                       else "07 Sep 2026"}
                             for f in view["fields"] if f["mine"]]})
    assert done["status"] == "completed"

    # ── 9. in force, and only because both signed ──────────────────────────
    rec = _get(f"/contracts/{cid}", carrier)
    assert rec["lifecycle"] == "active"
    assert sorted(g["side"] for g in rec["signatures"]) == ["carrier", "counterparty"]
    assert all(g["method"] == "typed" for g in rec["signatures"])
    assert rec["executed_date"]
    assert rec["unsigned_sides"] == []

    # Both links are dead. A signed contract cannot be reopened from an old
    # email on any device it was forwarded to.
    for tok in (mine["token"], hers["token"]):
        assert client.get(f"/esign/sign/{tok}").status_code == 404


def test_the_document_signed_is_the_document_downloaded(world):
    """The join that makes the round trustworthy: one wording, composed once.

    If these two ever came from different code the broker would agree to one
    document and sign another, and nothing in the app would notice."""
    carrier = world["carrier"]
    rec = _post("/contracts", carrier, {
        "program_id": world["program"], "contract_type": "insurer_broker",
        "name": "Schedule B — 2027", "counterparty_party_id": world["broker"],
        "class_of_business": "Commercial Auto",
        "inception_dt": "2027-01-01", "expiry_dt": "2027-12-31",
        "agreed_limits": {"commission_pct": {"value": "18"}},
        "wording_sections": [
            {"key": "financial", "title": "Financial terms", "origin": "generated",
             "body": "Commission is payable at {{commission_pct}}."}],
    })
    cid = rec["id"]
    _post(f"/contracts/{cid}/send-for-review", carrier, {})
    _post(f"/contracts/{cid}/accept-terms", world["brokerh"], {})

    read = client.get(f"/contracts/{cid}/contract.pdf", headers=carrier)
    assert read.status_code == 200

    s = _post(f"/esign/contracts/{cid}/signing-session", carrier)
    signable = client.get(f"/esign/sign/{s['token']}/pdf",
                          headers={"X-Esign-Session": s["session"]})
    assert signable.status_code == 200

    import fitz
    def words(data):
        with fitz.open(stream=data, filetype="pdf") as d:
            return "".join(pg.get_text() for pg in d).split()

    a, b = words(read.content), words(signable.content)
    # The signable copy carries the invisible anchors and nothing else extra.
    assert [w for w in a if "{{" not in w] == [w for w in b if "{{" not in w], (
        "the copy people read and the copy they sign must say the same thing")
    assert any("{{signature:" in w for w in b)
    assert not any("{{" in w for w in a), (
        "and the reading copy must carry no anchors at all")


def test_a_figure_typed_over_a_chip_is_tied_back_to_its_term(world):
    """The bug a carrier reported, in one test.

    The wording editor shows every term as a chip you cannot type by hand — but
    you can delete one and type the number it was showing, because on the screen
    it reads exactly the same. It is not the same contract: a chip moves when
    the term moves and a typed number does not, so the document goes on saying
    11% long after the commission was settled at 14%, while the check enforces
    14%. One contract, two answers.

    So it is tied back on the way in, and the tie is reported rather than done
    quietly — it changes the text of a contract.
    """
    carrier = world["carrier"]
    rec = _post("/contracts", carrier, {
        "program_id": world["program"], "contract_type": "insurer_broker",
        "name": "Schedule C — 2027", "counterparty_party_id": world["broker"],
        "class_of_business": "Commercial Auto",
        "inception_dt": "2027-01-01", "expiry_dt": "2027-12-31",
        "agreed_limits": {"commission_pct": {"value": "11"}},
        "wording_sections": [
            {"key": "financial", "title": "Financial terms", "origin": "generated",
             "body": "Commission is payable at {{commission_pct}}."}],
    })
    cid = rec["id"]

    # The carrier edits the clause and types the figure in place of the chip.
    r = client.patch(f"/contracts/{cid}", headers=carrier, json={
        "wording_sections": [
            {"key": "financial", "title": "Financial terms", "origin": "edited",
             "body": "Commission is payable at 11% of premium."}]})
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["wording_retied"] == ["Commission"], saved["wording_retied"]
    body = saved["wording_sections"][0]["body"]
    assert "{{commission_pct}}" in body, body
    # The words the carrier actually wrote are kept — only the figure moved.
    assert "of premium" in body

    # Now settle it at 14%, the way applying a change request does.
    r = client.patch(f"/contracts/{cid}", headers=carrier,
                     json={"agreed_limits": {"commission_pct": {"value": "14"}}})
    assert r.status_code == 200, r.text
    sec = r.json()["wording_sections"][0]
    assert "14%" in sec["rendered"], sec["rendered"]
    assert "11%" not in sec["rendered"]
    assert not r.json()["wording_unquoted"]

    # And the PDF says the same thing, because it is composed from the same
    # sections through the same tokens.
    import fitz
    pdf = client.get(f"/contracts/{cid}/contract.pdf", headers=carrier)
    assert pdf.status_code == 200
    with fitz.open(stream=pdf.content, filetype="pdf") as d:
        text = "".join(pg.get_text() for pg in d)
    assert "14%" in text and "11%" not in text


def test_a_term_the_wording_stopped_quoting_is_reported(world):
    """The case a re-tie cannot rescue: the clause was deleted outright. The
    term is still checked on every row and the document no longer says it —
    which nobody would notice unless the record says so."""
    carrier = world["carrier"]
    rec = _post("/contracts", carrier, {
        "program_id": world["program"], "contract_type": "insurer_broker",
        "name": "Schedule D — 2027", "counterparty_party_id": world["broker"],
        "class_of_business": "Commercial Auto",
        "inception_dt": "2027-01-01", "expiry_dt": "2027-12-31",
        "agreed_limits": {"commission_pct": {"value": "11"}},
        "wording_sections": [
            {"key": "financial", "title": "Financial terms", "origin": "generated",
             "body": "Commission is payable at {{commission_pct}}."}],
    })
    cid = rec["id"]
    assert not rec["wording_unquoted"]

    r = client.patch(f"/contracts/{cid}", headers=carrier, json={
        "wording_sections": [
            {"key": "financial", "title": "Financial terms", "origin": "edited",
             "body": "Accounts are settled as agreed between the parties."}]})
    assert r.status_code == 200, r.text
    flagged = r.json()["wording_unquoted"]
    assert [u["key"] for u in flagged] == ["commission_pct"], flagged
    assert flagged[0]["question"] == "Commission"
    assert flagged[0]["value"] == "11%"
