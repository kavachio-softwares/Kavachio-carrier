"""
What a signature block asks for — chosen per contract, not written in code.

WHAT CHANGED. Every contract Kavachio ever wrote got the same four lines on its
signature page — signature, full name, job title, date signed, for both sides —
because those four lines were literal text inside contract_wording. A carrier
who wanted initials on the block, or who did not want a job title box under a
name that already carries the title above it, had no way to say so. Changing it
meant changing that file and redeploying.

The lines are now DESCRIBED in one place (esign_pdf.SIGNATURE_BLOCK_FIELDS) and
CHOSEN per contract (`signature_layout`). Three things read that one list — the
form that offers the choice, the builder that draws the page, and the validator
that accepts it — so none of them can offer, draw or accept something the other
two cannot.

WHAT THESE TESTS HOLD.

  · The choice reaches the DOCUMENT. A box that is not on the page is not a box
    the signer is asked for, and the only proof of that is discover_fields
    reading the composed PDF back — which is what the signing round itself does.
  · A contract nobody has configured is byte-for-byte the contract it was
    before. Thousands of rows hold `{}` or `{"arrangement": "stacked"}`, and a
    change to how a block is described must not change what they produce.
  · The one line that cannot be turned off stays on. A block with nowhere to
    sign is not a signature block, and a document that reaches a signer with no
    signature box is a signing round that can never complete.
"""
import os
os.environ["MAIL_ALLOWED_RECIPIENTS"] = "nobody@example.invalid"
os.environ.setdefault("APP_BASE_URL", "http://localhost:5173")
import pytest
from fastapi.testclient import TestClient
import main
import contract_wording as cw
import esign_pdf
from auth_tokens import mint_access_token
from db import (AppUser, Contract, Party, Program, ProgramBroker, SessionLocal,
                Tenant)

client = TestClient(main.app)

ANCHORS = {"carrier": "tenant:1", "counterparty": "broker:2"}
SECTIONS = [{"title": "Cover", "body": "The Carrier covers the risks stated."}]


def _compose(layout, signers=None):
    return cw.compose_pdf(
        name="T", carrier_name="Insurisk", counterparty_name="CRC",
        sections=SECTIONS, tokens={}, schedule=[], signers=signers,
        signature_layout=layout, anchors=ANCHORS)


def _boxes(pdf) -> set[str]:
    """Every box the SIGNING ROUND would find. Read back out of the composed
    document rather than trusted from the layout — the layout is an intention,
    and this is what the signer is actually asked for."""
    return {f"{f.type}:{f.party_key}" for f in esign_pdf.discover_fields(pdf)}


@pytest.fixture(scope="module")
def w():
    sfx = os.urandom(4).hex()
    with SessionLocal() as s:
        car = Tenant(tenant_name=f"sigblk-{sfx}", legal_name="Insurisk")
        s.add(car); s.commit()
        br = Party(tenant_id=car.id, party_type="broker", legal_name="CRC",
                   reference=f"sigblk-b-{sfx}", is_active=True)
        s.add(br); s.commit()
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
        # A seat on the broker's side, so a round can be opened without naming
        # signers on the request — the envelope resolves this one.
        marco = AppUser(tenant_id=None, broker_party_id=br.id,
                        email=f"m-{sfx}@crc.test", full_name="Marco",
                        role="broker_admin", invited_by_user_id=dana.id)
        s.add(marco); s.commit()
        return {"t": car.id, "b": br.id, "p": pr.id,
                "dana": dana.email, "marco": marco.email,
                "ch": {"Authorization":
                       f"Bearer {mint_access_token(dana.id, car.id, 'carrier_admin')}"}}


def _create(w, layout, **over):
    body = {"program_id": w["p"], "contract_type": "insurer_broker",
            "counterparty_party_id": w["b"], "name": f"B {os.urandom(2).hex()}",
            "class_of_business": "Property", "create_as": "draft",
            "inception_dt": "2027-01-01", "expiry_dt": "2027-12-31",
            "wording_sections": SECTIONS, "signature_layout": layout}
    body.update(over)
    return client.post("/contracts", headers=w["ch"], json=body)


# ── the vocabulary is served, not restated ─────────────────────────────────
def test_the_form_is_told_what_a_block_may_contain(w):
    """The form is built from this. If it stops being served the screen has
    nothing to offer, which is a worse failure than a wrong default."""
    r = client.get("/contract-types", headers=w["ch"])
    assert r.status_code == 200, r.text
    spec = r.json()["signature_block"]

    keys = [f["key"] for f in spec["fields"]]
    assert "signature" in keys
    # Every offered line has to be a type the PDF layer can actually place, or
    # the form offers a box that silently never appears.
    assert set(keys) <= set(esign_pdf.FIELD_TYPES)
    assert [f["key"] for f in spec["fields"] if f["fixed"]] == ["signature"]
    assert {a["key"] for a in spec["arrangements"]} >= {"side_by_side", "stacked"}
    assert spec["sides"] == ["carrier", "counterparty"]
    # And a starting layout, so the form never has to invent one.
    assert spec["default"]["fields"]["carrier"]


# ── the choice reaches the document ────────────────────────────────────────
def test_an_unconfigured_contract_gets_exactly_what_it_always_got():
    """The regression that matters most. Rows written before this existed hold
    {} — and {} must still mean the four lines every contract has had."""
    was = {f"{k}:{p}" for p in ("tenant:1", "broker:2")
           for k in ("signature", "name", "title", "date")}
    assert _boxes(_compose(None)) == was
    assert _boxes(_compose({})) == was
    # Including the one shape that WAS honoured before — arrangement only.
    assert _boxes(_compose({"arrangement": "stacked"})) == was


def test_unticking_a_line_takes_the_box_off_the_page():
    """Not greyed out, not left blank — absent. A box on the page is a box the
    signer must fill, so 'we do not want a job title' has to mean there is
    nothing there to fill."""
    pdf = _compose({"fields": {"carrier": ["signature", "name"],
                               "counterparty": ["signature", "name"]}})
    assert _boxes(pdf) == {"signature:tenant:1", "name:tenant:1",
                           "signature:broker:2", "name:broker:2"}


def test_a_line_can_be_added_that_no_contract_had_before():
    """Initials were in the vocabulary and unreachable — nothing wrote the
    anchor, so no contract could ever ask for them."""
    pdf = _compose({"fields": {"carrier": ["signature", "initial", "date"],
                               "counterparty": ["signature"]}})
    assert "initial:tenant:1" in _boxes(pdf)
    assert "initial:broker:2" not in _boxes(pdf), "one side's choice is not both"


def test_the_two_sides_are_chosen_separately():
    """A treaty signed by a director on one side and an attorney on the other
    does not need the same lines under each."""
    pdf = _compose({"fields": {"carrier": ["signature", "title"],
                               "counterparty": ["signature", "name", "date"]}})
    assert _boxes(pdf) == {"signature:tenant:1", "title:tenant:1",
                           "signature:broker:2", "name:broker:2", "date:broker:2"}


def test_tick_order_does_not_change_the_page():
    """Two carriers who tick the same boxes in a different order must get the
    same document. The block reads in the vocabulary's order, not theirs."""
    a = _compose({"fields": {"carrier": ["date", "signature", "name"],
                             "counterparty": ["signature"]}})
    b = _compose({"fields": {"carrier": ["name", "date", "signature"],
                             "counterparty": ["signature"]}})
    assert _boxes(a) == _boxes(b)


def test_a_named_signatory_is_printed_and_not_asked(w):
    """Name and title are typed by the CARRIER at step 4. Asking the signer for
    them again is a form, not a signature — so those two lines print instead of
    becoming boxes, and the signature and date still do not."""
    pdf = _compose(None, signers=[{"side": "carrier", "name": "Dana Alvarez",
                                   "role": "Head of Underwriting"}])
    got = _boxes(pdf)
    assert "signature:tenant:1" in got and "date:tenant:1" in got
    assert "name:tenant:1" not in got and "title:tenant:1" not in got
    # The other side was not named, so it keeps its boxes.
    assert {"name:broker:2", "title:broker:2"} <= got


# ── what it refuses ────────────────────────────────────────────────────────
def test_a_block_with_nowhere_to_sign_is_refused(w):
    """The one line that cannot come off. A document that reached a signer with
    no signature box would be a round nobody could ever complete, and it would
    only be discovered by the person trying to sign it."""
    r = _create(w, {"fields": {"carrier": ["name", "date"],
                               "counterparty": ["signature"]}})
    assert r.status_code == 400, r.text
    # Named by SIDE, so the form can mark the column that is wrong.
    assert "carrier" in r.json()["detail"]["errors"]


def test_a_line_that_does_not_exist_is_refused(w):
    r = _create(w, {"fields": {"carrier": ["signature", "fingerprint"],
                               "counterparty": ["signature"]}})
    assert r.status_code == 400, r.text


def test_an_arrangement_that_cannot_be_drawn_is_refused(w):
    r = _create(w, {"arrangement": "diagonal",
                    "fields": {"carrier": ["signature"],
                               "counterparty": ["signature"]}})
    assert r.status_code == 400, r.text


def test_a_side_left_out_keeps_its_own_block(w):
    """Half a layout is the form sending half of itself, not a request for an
    empty block. The missing side falls back rather than losing its lines."""
    r = _create(w, {"fields": {"carrier": ["signature"]}})
    assert r.status_code == 200, r.text
    stored = client.get(f"/contracts/{r.json()['id']}",
                        headers=w["ch"]).json()["signature_layout"]
    assert stored["fields"]["counterparty"] == ["signature", "name", "title", "date"]


# ── it survives the round trip ─────────────────────────────────────────────
def test_the_choice_is_stored_and_comes_back(w):
    chosen = {"arrangement": "stacked",
              "fields": {"carrier": ["signature", "initial"],
                         "counterparty": ["signature", "date"]}}
    r = _create(w, chosen)
    assert r.status_code == 200, r.text
    back = client.get(f"/contracts/{r.json()['id']}",
                      headers=w["ch"]).json()["signature_layout"]
    # What was ASKED FOR comes back unchanged. Not compared whole: a layout
    # carries everything the document builder needs, which is more than a form
    # sends — `blocks` is where the two hand-placed blocks sit, and a contract
    # that never placed one gets an empty one rather than no answer.
    assert back["arrangement"] == chosen["arrangement"]
    assert back["fields"] == chosen["fields"]
    assert back["blocks"] == {}


def test_it_can_be_changed_afterwards_and_still_checked(w):
    r = _create(w, None)
    cid = r.json()["id"]
    ok = client.patch(f"/contracts/{cid}", headers=w["ch"], json={
        "signature_layout": {"arrangement": "stacked",
                             "fields": {"carrier": ["signature"],
                                        "counterparty": ["signature"]}}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["signature_layout"]["arrangement"] == "stacked"

    bad = client.patch(f"/contracts/{cid}", headers=w["ch"], json={
        "signature_layout": {"fields": {"carrier": [], "counterparty": []}}})
    assert bad.status_code == 400, bad.text


def test_the_signing_round_asks_for_what_was_chosen(w):
    """End to end, and the only test that proves the feature works: the boxes a
    real signer is shown come from the layout the carrier ticked."""
    r = _create(w, {"fields": {"carrier": ["signature", "date"],
                               "counterparty": ["signature", "date"]}})
    cid = r.json()["id"]
    assert client.post(f"/contracts/{cid}/skip-review",
                       headers=w["ch"], json={}).status_code == 200

    env = client.post("/esign/envelopes", headers=w["ch"], json={
        "contract_id": cid, "source": "contract", "send_now": False})
    assert env.status_code == 200, env.text
    kinds = {f["type"] for f in env.json()["fields"]}
    assert kinds == {"signature", "date"}, kinds


# ── the lines line up ──────────────────────────────────────────────────────
def test_the_lines_under_a_rule_all_start_at_one_x():
    """Two columns, not one run of text.

    Every line under a rule used to start its box at the end of its own label,
    so "Initials:" and "Date signed:" put their boxes a third of an inch apart
    and the block read as though it had been thrown at the page. The labels are
    printed in one column now and everything beside them in another, which is
    the only thing that makes four boxes look like one block.
    """
    pdf = _compose({"fields": {"carrier": ["signature", "name", "title",
                                           "date", "initial"],
                               "counterparty": ["signature", "date"]}})
    xs = {round(f.x, 4) for f in esign_pdf.discover_fields(pdf)
          if f.party_key == "tenant:1" and f.type != "signature"}
    assert len(xs) == 1, f"the boxes under one rule start at {sorted(xs)}"


def test_no_box_reaches_into_the_row_below_it():
    """A box owns its own line and no part of anybody else's.

    Two ways it stopped doing that. Initials were given a box twice the height
    of the rows around them and drawn bottom-aligned, so they printed well
    under their own label. And the signature was given more height than there
    is room between its anchor and the ruled line, so it ran past the rule and
    onto "Full name:" — a signature written through the next label, under a
    clickable box covering a row it does not own.

    Both were invisible to every other test here, which asks WHICH boxes exist
    and never where they land. This one measures.
    """
    fields = ["signature", "name", "title", "date", "initial"]
    got = [f for f in esign_pdf.discover_fields(
        _compose({"fields": {"carrier": fields, "counterparty": fields}}))
        if f.party_key == "tenant:1"]
    assert len(got) == len(fields)
    for above, below in zip(got, got[1:]):
        assert above.y + above.h <= below.y, (
            f"the {above.type} box runs into the {below.type} row beneath it")
    # Every row under the rule is one line tall — the same line, so the block
    # reads as a block. Only the signature, which sits above a rule, differs.
    heights = {round(f.h, 6) for f in got if f.type != "signature"}
    assert len(heights) == 1, f"the rows under one rule are {sorted(heights)} tall"


# ── more than two people ───────────────────────────────────────────────────
def test_a_side_that_sends_two_people_gets_two_sets_of_boxes():
    """One organisation, two signatories, two places to sign.

    A second name under one rule is one signature block with two names in it,
    which is not what a second signatory is. Each gets a rule and boxes keyed to
    THEM — the slot on the party key — so nobody can fill anybody else's.
    """
    got = _boxes(_compose(None, signers=[
        {"side": "carrier", "name": "Dana Alvarez", "email": "d@x.test"},
        {"side": "carrier", "name": "Sam Okafor", "email": "s@x.test"},
        {"side": "counterparty", "name": "Marco Diaz", "email": "m@crc.test"}]))
    assert "signature:tenant:1" in got
    assert "signature:tenant:1#2" in got
    assert "signature:broker:2" in got
    # Still nobody's third block: two were named, two were drawn.
    assert "signature:tenant:1#3" not in got


def test_somebody_given_no_access_is_printed_and_never_asked():
    """The carrier's answer to "this person is outside Kavachio".

    Said no, their lines are printed on the signature page with nothing to
    click on and they sign the paper copy — which is how plenty of people named
    on a contract have always signed it. The block is still drawn; only the
    boxes are missing.
    """
    got = _boxes(_compose(None, signers=[
        {"side": "carrier", "name": "Dana Alvarez", "email": "d@x.test"},
        {"side": "carrier", "name": "Outside Counsel", "email": "c@law.test",
         "access": False},
        {"side": "counterparty", "name": "Marco Diaz", "email": "m@crc.test"}]))
    assert "signature:tenant:1" in got
    assert not any(k.endswith("tenant:1#2") for k in got), got


def test_the_round_makes_a_signer_of_everybody_the_page_asks_for(w):
    """End to end. Three people named, three links to issue, in the order they
    were named — and the boxes on the page are shared out between them."""
    r = _create(w, None, signers=[
        {"side": "carrier", "name": "Dana Alvarez", "role": "Underwriting",
         "email": w["dana"]},
        {"side": "carrier", "name": "Sam Okafor", "role": "Chair",
         "email": "sam@x.test"},
        {"side": "counterparty", "name": "Marco Diaz", "role": "Broker",
         "email": w["marco"]}])
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    assert client.post(f"/contracts/{cid}/skip-review",
                       headers=w["ch"], json={}).status_code == 200

    env = client.post("/esign/envelopes", headers=w["ch"], json={
        "contract_id": cid, "source": "contract", "send_now": False})
    assert env.status_code == 200, env.text
    body = env.json()
    keys = [x["party_key"] for x in body["recipients"]]
    assert keys == [f"tenant:{w['t']}", f"tenant:{w['t']}#2", f"broker:{w['b']}"]
    # Asked in the order they were named, this side before the other.
    assert [x["order"] for x in body["recipients"]] == [1, 2, 3]
    # And every one of them has somewhere to sign.
    owned = {f["party_key"] for f in body["fields"]}
    assert owned == set(keys), owned


def test_a_signatory_with_no_access_gets_no_link(w):
    """Printed on the page, and not on the round. A recipient row for somebody
    with nothing to fill would be a round waiting on a person who was never
    given a way in."""
    r = _create(w, None, signers=[
        {"side": "carrier", "name": "Dana Alvarez", "email": w["dana"]},
        {"side": "carrier", "name": "Outside Counsel", "email": "c@law.test",
         "access": False},
        {"side": "counterparty", "name": "Marco Diaz", "email": w["marco"]}])
    cid = r.json()["id"]
    assert client.post(f"/contracts/{cid}/skip-review",
                       headers=w["ch"], json={}).status_code == 200
    body = client.post("/esign/envelopes", headers=w["ch"], json={
        "contract_id": cid, "source": "contract", "send_now": False}).json()
    keys = [x["party_key"] for x in body["recipients"]]
    assert keys == [f"tenant:{w['t']}", f"broker:{w['b']}"], keys


# ── is this address one of ours? ───────────────────────────────────────────
def test_an_address_we_know_is_named_and_one_we_do_not_is_flagged(w):
    """The question the screen puts before a name is added.

    Scoped to the contract's own two organisations, so it answers "we know
    them" or "we do not" and can never be used to go fishing for who else holds
    an account here.
    """
    cid = _create(w, None).json()["id"]

    mine = client.get(f"/esign/contracts/{cid}/signer-lookup",
                      headers=w["ch"], params={"email": w["marco"]})
    assert mine.status_code == 200, mine.text
    assert mine.json()["known"] is True
    assert mine.json()["side"] == "counterparty"

    them = client.get(f"/esign/contracts/{cid}/signer-lookup",
                      headers=w["ch"], params={"email": "someone@law.test"})
    assert them.status_code == 200, them.text
    assert them.json()["known"] is False
