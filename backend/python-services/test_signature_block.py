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
import fitz
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
                       f"Bearer {mint_access_token(dana.id, car.id, 'carrier_admin')}"},
                "bh": {"Authorization":
                       f"Bearer {mint_access_token(marco.id, None, 'broker_admin')}"}}


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


# ── a block per PERSON ─────────────────────────────────────────────────────
#
# Three people signing for one side used to mean one dragged point and three
# rules stacked under it. These hold that each of them can be dragged somewhere
# of their own instead — and that not dragging them still stacks, because the
# old behaviour is what you get by not using the new one.

def _placed_at(**blocks):
    return {"arrangement": "placed",
            "fields": {s: list(esign_pdf.DEFAULT_SIGNATURE_FIELDS)
                       for s in esign_pdf.SIGNATURE_SIDES},
            "blocks": blocks}


def _sig_spots(pdf) -> dict:
    """Where each party key's SIGNATURE box actually landed, off the page."""
    return {f.party_key: (f.page, round(f.x, 3), round(f.y, 3))
            for f in esign_pdf.discover_fields(pdf) if f.type == "signature"}


THREE = [{"side": "carrier", "name": "Giri", "role": "Carrier Admin"},
         {"side": "carrier", "name": "Asha", "role": "Director"},
         {"side": "carrier", "name": "Ravi", "role": "Finance"},
         {"side": "counterparty", "name": "Xemo", "role": "Broker"}]


def test_three_signatories_can_be_dragged_to_three_different_places():
    """The whole point. Each named person's boxes land where THEIR block was
    left, not stacked under the first one's."""
    pdf = _compose(_placed_at(**{
        "carrier": {"page": 1, "x": 0.08, "y": 0.20},
        "carrier#2": {"page": 1, "x": 0.55, "y": 0.45},
        "carrier#3": {"page": 1, "x": 0.08, "y": 0.70},
        "counterparty": {"page": 1, "x": 0.55, "y": 0.70}}), signers=THREE)
    spots = _sig_spots(pdf)
    assert set(spots) == {"tenant:1", "tenant:1#2", "tenant:1#3", "broker:2"}
    # Four boxes, four places — no two people asked to sign the same spot.
    assert len(set(spots.values())) == 4
    assert spots["tenant:1"][1] == pytest.approx(0.08, abs=0.01)
    assert spots["tenant:1#2"][1] == pytest.approx(0.55, abs=0.01)
    # A rule sits a heading below the corner it was dropped on — the block
    # prints "For the Carrier" and the organisation above it. What matters is
    # that the drop is HONOURED, so the gap is the same for every block rather
    # than the rule landing exactly on the point.
    drops = {"tenant:1": 0.20, "tenant:1#2": 0.45, "tenant:1#3": 0.70}
    gaps = {k: spots[k][2] - y for k, y in drops.items()}
    assert max(gaps.values()) - min(gaps.values()) < 0.005, gaps
    assert 0 < min(gaps.values()) < 0.06, gaps


def test_a_signatory_nobody_dragged_still_stacks_under_their_side():
    """Placing is per person and OPTIONAL. A layout that names three and places
    only the side is the behaviour every placed contract had before this."""
    pdf = _compose(_placed_at(
        carrier={"page": 1, "x": 0.08, "y": 0.20},
        counterparty={"page": 1, "x": 0.55, "y": 0.70}), signers=THREE)
    spots = _sig_spots(pdf)
    assert set(spots) == {"tenant:1", "tenant:1#2", "tenant:1#3", "broker:2"}
    # One column: same x, each one lower than the last.
    col = [spots[k] for k in ("tenant:1", "tenant:1#2", "tenant:1#3")]
    assert len({p[1] for p in col}) == 1
    assert [p[2] for p in col] == sorted(p[2] for p in col)


def test_placing_one_person_leaves_the_rest_stacked_where_they_were():
    """The two are not modes. A side can have one signatory lifted out to a
    corner of its own while the others stay in the column."""
    pdf = _compose(_placed_at(**{
        "carrier": {"page": 1, "x": 0.08, "y": 0.20},
        "carrier#3": {"page": 1, "x": 0.60, "y": 0.30},
        "counterparty": {"page": 1, "x": 0.55, "y": 0.70}}), signers=THREE)
    spots = _sig_spots(pdf)
    # 1 and 2 still share the side's column; 3 went where it was dropped.
    assert spots["tenant:1"][1] == spots["tenant:1#2"][1]
    assert spots["tenant:1#3"][1] == pytest.approx(0.60, abs=0.01)
    # Lifted out of the column: above signatory 1, though dropped lower on the
    # page than the side block, because it is a block of its own now.
    assert spots["tenant:1#3"][2] == pytest.approx(0.30 + 0.032, abs=0.01)
    assert spots["tenant:1#3"][2] > spots["tenant:1"][2]


# ── whose blocks these are ─────────────────────────────────────────────────
#
# Placing the blocks is part of AUTHORING the contract, and the broker's part
# in signing it is to sign. A broker who could drag the blocks could move the
# carrier's signature somewhere the carrier never agreed to put it — on a
# document the carrier is about to be bound by — so the answer is the same one
# naming the signatories gets, and for the same reason.

def test_only_the_carrier_may_place_the_blocks(w):
    """The broker cannot move a block, on a contract they are party to."""
    cid = _create(w, _placed_at(
        carrier={"page": 1, "x": 0.08, "y": 0.20},
        counterparty={"page": 1, "x": 0.55, "y": 0.70})).json()["id"]

    moved = _placed_at(carrier={"page": 1, "x": 0.80, "y": 0.90},
                       counterparty={"page": 1, "x": 0.55, "y": 0.70})
    r = client.patch(f"/contracts/{cid}", headers=w["bh"],
                     json={"signature_layout": moved})
    assert r.status_code == 403, r.text

    # And the placement is untouched — refused, not quietly half-applied.
    got = client.get(f"/contracts/{cid}", headers=w["ch"]).json()
    assert got["signature_layout"]["blocks"]["carrier"]["x"] == 0.08


def test_the_carrier_may_move_them(w):
    """The same request from the seat that owns it goes through — otherwise the
    test above would pass on a contract nobody can place blocks on at all."""
    cid = _create(w, _placed_at(
        carrier={"page": 1, "x": 0.08, "y": 0.20},
        counterparty={"page": 1, "x": 0.55, "y": 0.70})).json()["id"]

    moved = _placed_at(**{"carrier": {"page": 1, "x": 0.30, "y": 0.40},
                          "carrier#2": {"page": 1, "x": 0.60, "y": 0.55},
                          "counterparty": {"page": 1, "x": 0.55, "y": 0.70}})
    r = client.patch(f"/contracts/{cid}", headers=w["ch"],
                     json={"signature_layout": moved})
    assert r.status_code == 200, r.text
    blocks = r.json()["signature_layout"]["blocks"]
    assert blocks["carrier"]["x"] == 0.30
    assert blocks["carrier#2"] == {"page": 1, "x": 0.6, "y": 0.55}


# ── placing the blocks BEFORE the contract exists ──────────────────────────
#
# The create wizard used to have no way to place blocks by hand: there was no
# contract to drag onto, so the choice was offered only on the saved record and
# a carrier who wanted a block somewhere particular had to create the contract,
# leave the flow and go and move it. The document was composable from the
# builder's own state the whole time — that is what "Download the draft" does —
# so these serve it as PAGES, saved nowhere.

def _wizard_body(layout=None):
    return {"contract_type": "insurer_broker",
            "values": {"name": "Unsaved"},
            "agreed_limits": {},
            "sections": SECTIONS,
            "carrier_name": "Insurisk",
            "counterparty_name": "CRC",
            "signature_layout": layout}


def test_the_pages_of_an_unsaved_contract_can_be_counted(w):
    r = client.post("/contract-wording/pages", headers=w["ch"],
                    json=_wizard_body())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pages"] >= 1
    assert len(body["sizes"]) == body["pages"]
    assert body["sizes"][0]["width"] > 0 and body["sizes"][0]["height"] > 0


def test_a_page_of_an_unsaved_contract_is_drawn(w):
    r = client.post("/contract-wording/pages/1", headers=w["ch"],
                    json=_wizard_body(), params={"scale": 1.0})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    # Never cached: the wording is not a file, so a cached image is a picture
    # of terms that may have moved since.
    assert "no-store" in r.headers.get("cache-control", "")


def test_a_page_that_is_not_there_is_a_404_not_a_broken_image(w):
    r = client.post("/contract-wording/pages/99", headers=w["ch"],
                    json=_wizard_body())
    assert r.status_code == 404, r.text


def test_the_page_count_is_the_same_whichever_arrangement_is_asked_for(w):
    """What makes placing in the wizard SAFE. A block is stored against a page
    number, and the pages dragged onto are composed with an automatic
    arrangement — so if placing changed the page count, every position chosen
    here would point at the wrong page of the contract that got saved."""
    counts = set()
    for lay in (None,
                {"arrangement": "side_by_side", "fields": {}},
                {"arrangement": "stacked", "fields": {}},
                _placed_at(carrier={"page": 1, "x": 0.08, "y": 0.6},
                           counterparty={"page": 1, "x": 0.55, "y": 0.6})):
        r = client.post("/contract-wording/pages", headers=w["ch"],
                        json=_wizard_body(lay))
        assert r.status_code == 200, r.text
        counts.add(r.json()["pages"])
    assert len(counts) == 1, counts


def test_nothing_is_written_by_looking_at_the_pages(w):
    """"Nothing has been sent yet" has to stay true while somebody is still
    building one."""
    from db import Contract
    with SessionLocal() as s:
        before = s.query(Contract).count()
    client.post("/contract-wording/pages", headers=w["ch"], json=_wizard_body())
    client.post("/contract-wording/pages/1", headers=w["ch"], json=_wizard_body())
    with SessionLocal() as s:
        assert s.query(Contract).count() == before


def test_a_wizard_with_no_wording_yet_is_told_so(w):
    r = client.post("/contract-wording/pages", headers=w["ch"],
                    json={**_wizard_body(), "sections": []})
    assert r.status_code == 400, r.text


# ── a block that cannot cover the wording ──────────────────────────────────
#
# A coordinate is a point on a FINISHED page: the block is drawn on top, and
# nothing stops one landing over a clause. An anchor is a place in the
# CONTRACT: the block joins the flow after clause n and ReportLab typesets it,
# so the clauses below move down and room is made. The two are different
# answers to "where does this go" and both are offered, because neither is the
# whole answer — a coordinate can put one person's boxes in a margin, and an
# anchor can promise the words are never covered.

def _tied(after, **rest):
    return {"arrangement": "placed",
            "fields": {s: list(esign_pdf.DEFAULT_SIGNATURE_FIELDS)
                       for s in esign_pdf.SIGNATURE_SIDES},
            "blocks": {"carrier": {"after": after}, **rest}}


LONG = [{"title": f"Clause {i}",
         "body": "The Carrier covers the risks stated. " * 30}
        for i in range(1, 6)]


def _compose_long(layout, signers=None):
    return cw.compose_pdf(
        name="T", carrier_name="Insurisk", counterparty_name="CRC",
        sections=LONG, tokens={}, schedule=[], signers=signers,
        signature_layout=layout, anchors=ANCHORS)


def _reading_order(pdf, page_no):
    """What is on a page, top to bottom, the way somebody reads it."""
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        pg = doc[page_no - 1]
        return [(b[4] or "").strip().replace("\n", " ")
                for b in sorted(pg.get_text("blocks"), key=lambda b: b[1])
                if (b[4] or "").strip()]
    finally:
        doc.close()


def test_a_block_tied_to_a_clause_is_typeset_between_it_and_the_next_one():
    """The whole point of anchoring. The block is IN the document, so what
    follows it moves down rather than being covered."""
    lay = esign_pdf.normalise_signature_layout(
        _tied(2, counterparty={"page": 1, "x": 0.55, "y": 0.90}), strict=True)
    pdf = _compose_long(lay)
    order = _reading_order(pdf, 1)
    where = next(i for i, t in enumerate(order) if t.startswith("For the Carrier"))
    before = [t for t in order[:where] if t.startswith(("1.", "2.", "3."))]
    after = [t for t in order[where + 1:] if t.startswith(("1.", "2.", "3."))]
    assert before and before[-1].startswith("2."), order
    # Clause 3 is BELOW it — on this page or pushed to the next, never over it.
    assert not any(t.startswith(("1.", "2.")) for t in after), order


def test_nothing_is_typeset_on_top_of_an_anchored_block():
    """A flowable cannot be overlapped, and this is the test that says so —
    the property the coordinate blocks cannot offer."""
    lay = esign_pdf.normalise_signature_layout(_tied(2, counterparty={
        "page": 1, "x": 0.55, "y": 0.90}), strict=True)
    doc = fitz.open(stream=_compose_long(lay), filetype="pdf")
    try:
        pg = doc[0]
        blk = next(b for b in pg.get_text("blocks")
                   if "For the Carrier" in (b[4] or ""))
        clash = [b for b in pg.get_text("blocks")
                 if (b[4] or "").strip() and b[:4] != blk[:4]
                 and not (b[3] <= blk[1] or b[1] >= blk[3])
                 and not (b[2] <= blk[0] or b[0] >= blk[2])]
        assert clash == [], [c[4] for c in clash]
    finally:
        doc.close()


def test_an_anchored_side_is_not_also_drawn_on_the_signature_page():
    """One block per side, wherever it went. Drawn twice, the contract would
    have two places for the same signature and no way to say which counts."""
    lay = esign_pdf.normalise_signature_layout(
        _tied(2, counterparty={"page": 1, "x": 0.55, "y": 0.90}), strict=True)
    pdf = _compose_long(lay)
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        seen = sum(pg.get_text().count("For the Carrier") for pg in doc)
    finally:
        doc.close()
    assert seen == 1, seen
    # And it still has exactly one set of signing boxes.
    keys = [f.party_key for f in esign_pdf.discover_fields(pdf)
            if f.type == "signature"]
    assert sorted(keys) == ["broker:2", "tenant:1"], keys


def test_both_sides_may_be_tied_and_the_signature_page_survives_empty():
    lay = esign_pdf.normalise_signature_layout(
        {"arrangement": "placed",
         "fields": {s: list(esign_pdf.DEFAULT_SIGNATURE_FIELDS)
                    for s in esign_pdf.SIGNATURE_SIDES},
         "blocks": {"carrier": {"after": 1}, "counterparty": {"after": 3}}},
        strict=True)
    pdf = _compose_long(lay)
    keys = sorted(f.party_key for f in esign_pdf.discover_fields(pdf)
                  if f.type == "signature")
    assert keys == ["broker:2", "tenant:1"], keys


def test_a_side_tied_past_the_last_clause_falls_to_the_signature_page():
    """Forgiving, like a block on a page that no longer exists: a contract
    with nowhere to sign is the one failure worse than the wrong layout."""
    lay = esign_pdf.normalise_signature_layout(
        _tied(99, counterparty={"page": 1, "x": 0.55, "y": 0.90}), strict=True)
    pdf = _compose_long(lay)
    keys = sorted(f.party_key for f in esign_pdf.discover_fields(pdf)
                  if f.type == "signature")
    assert keys == ["broker:2", "tenant:1"], keys


def test_the_canvas_leaves_the_signature_page_blank(w):
    """What is dragged onto. The automatic blocks must NOT be printed on it:
    they are the thing being positioned, and a page that already shows them
    makes the words they sit on look like space that is taken."""
    body = _wizard_body()
    r = client.post("/contract-wording/pages", headers=w["ch"], json=body)
    assert r.status_code == 200, r.text
    # The last page carries the heading and the sentence, and no block.
    assert len(r.json()["text"][-1]) <= 3, r.json()["text"][-1]


def test_the_canvas_says_where_the_words_are(w):
    r = client.post("/contract-wording/pages", headers=w["ch"],
                    json=_wizard_body())
    text = r.json()["text"]
    assert any(page for page in text), "no text regions at all"
    for page in text:
        for box in page:
            assert 0 <= box["x"] <= 1 and 0 <= box["y"] <= 1, box
            assert box["w"] > 0 and box["h"] > 0, box
            assert box["x"] + box["w"] <= 1.001, box


def test_the_clauses_offered_are_the_clauses_composed(w):
    """Numbered off the same list the composer typesets — a section with no
    body never reaches the page, so it is not a clause and has no number."""
    body = _wizard_body()
    body["sections"] = [{"title": "Real", "body": "Something."},
                        {"title": "Empty", "body": "   "},
                        {"title": "Also real", "body": "Something else."}]
    r = client.post("/contract-wording/pages", headers=w["ch"], json=body)
    assert r.status_code == 200, r.text
    assert r.json()["clauses"] == [{"n": 1, "title": "Real"},
                                   {"n": 2, "title": "Also real"}], r.json()


def test_a_saved_contract_can_be_shown_as_a_layout_would_compose_it(w):
    """Anchoring is only a choice you can make if you can see it happen."""
    cid = _create(w, None).json()["id"]
    plain = client.post(f"/contracts/{cid}/pages", headers=w["ch"],
                        json={}).json()
    tied = client.post(f"/contracts/{cid}/pages", headers=w["ch"], json={
        "signature_layout": _tied(1, counterparty={"page": 1, "x": .5, "y": .8})
    }).json()
    assert plain["clauses"], plain
    # The document really is composed differently — the block moved into it.
    assert tied["text"] != plain["text"]


# ── dropped on a page, and the wording moves down for it ───────────────────
#
# What "a point on the page" means now. A block used to be drawn ON TOP of the
# finished page, so on a full page it landed across the middle of a clause —
# the one thing a signature must never do. A drop is resolved instead to a
# place in the FLOW: the mark just above it, plus how far below that mark it
# fell. The block is typeset there, so the wording moves down to make room,
# and covering a clause stops being possible rather than being warned about.

def _drop(marks, page, y, x, block_h=0.13):
    """What the screen does with a drop — the mark above it, and the gap."""
    above = [m for m in marks if m["page"] < page
             or (m["page"] == page and m["y"] <= y)]
    m = above[-1] if above else marks[0]
    return {"at": m["n"],
            "gap": round(max(0.0, y - m["y"]), 5) if m["page"] == page else 0.0,
            "x": round(x, 5)}


def _canvas_marks(signers=None):
    marks: list = []
    cw.compose_pdf(
        name="T", carrier_name="Insurisk", counterparty_name="CRC",
        sections=LONG, tokens={}, schedule=[], signers=signers,
        signature_layout={"arrangement": "side_by_side",
                          "fields": {s: list(esign_pdf.DEFAULT_SIGNATURE_FIELDS)
                                     for s in esign_pdf.SIGNATURE_SIDES}},
        anchors=ANCHORS, blank_signature_space=True, marks_out=marks)
    return marks


def _boxes_on(pdf, page_no):
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        pg = doc[page_no - 1]
        return pg.rect, [b for b in pg.get_text("blocks") if (b[4] or "").strip()]
    finally:
        doc.close()


def test_a_dropped_block_lands_where_it_was_dropped():
    """The translation has to be exact, or dragging would not look like
    dragging — the box would jump out from under the pointer."""
    marks = _canvas_marks()
    want_y, want_x = 0.62, 0.55
    lay = esign_pdf.normalise_signature_layout(
        {"arrangement": "placed",
         "fields": {s: list(esign_pdf.DEFAULT_SIGNATURE_FIELDS)
                    for s in esign_pdf.SIGNATURE_SIDES},
         "blocks": {"carrier": _drop(marks, 1, want_y, want_x),
                    "counterparty": {"after": 4}}}, strict=True)
    pdf = _compose_long(lay)
    rect, boxes = _boxes_on(pdf, 1)
    blk = next(b for b in boxes if "For the Carrier" in (b[4] or ""))
    assert blk[1] / rect.height == pytest.approx(want_y, abs=0.01)
    assert blk[0] / rect.width == pytest.approx(want_x, abs=0.02)


def test_the_wording_moves_down_instead_of_being_covered():
    """The whole point. Dropped in the middle of a full page of clauses, the
    block covers none of them — because it is IN the document, not on it."""
    marks = _canvas_marks()
    lay = esign_pdf.normalise_signature_layout(
        {"arrangement": "placed",
         "fields": {s: list(esign_pdf.DEFAULT_SIGNATURE_FIELDS)
                    for s in esign_pdf.SIGNATURE_SIDES},
         "blocks": {"carrier": _drop(marks, 1, 0.45, 0.1),
                    "counterparty": {"after": 4}}}, strict=True)
    pdf = _compose_long(lay)
    rect, boxes = _boxes_on(pdf, 1)
    blk = next(b for b in boxes if "For the Carrier" in (b[4] or ""))
    clash = [b for b in boxes if b[:4] != blk[:4]
             and not (b[3] <= blk[1] or b[1] >= blk[3])
             and not (b[2] <= blk[0] or b[0] >= blk[2])]
    assert clash == [], [c[4] for c in clash]


def test_a_dropped_side_is_not_also_drawn_on_the_signature_page():
    marks = _canvas_marks()
    lay = esign_pdf.normalise_signature_layout(
        {"arrangement": "placed",
         "fields": {s: list(esign_pdf.DEFAULT_SIGNATURE_FIELDS)
                    for s in esign_pdf.SIGNATURE_SIDES},
         "blocks": {"carrier": _drop(marks, 1, 0.45, 0.1),
                    "counterparty": _drop(marks, 1, 0.80, 0.55)}}, strict=True)
    pdf = _compose_long(lay)
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        assert sum(pg.get_text().count("For the Carrier") for pg in doc) == 1
        assert sum(pg.get_text().count("For the Counterparty") for pg in doc) == 1
    finally:
        doc.close()
    keys = sorted(f.party_key for f in esign_pdf.discover_fields(pdf)
                  if f.type == "signature")
    assert keys == ["broker:2", "tenant:1"], keys


def test_each_person_dropped_gets_only_their_own_rule():
    """Three people, three drops, three blocks — and the block dropped for the
    second person must carry the second person, not all three of them."""
    three = [{"side": "carrier", "name": "Giri", "role": "Carrier Admin"},
             {"side": "carrier", "name": "Asha", "role": "Director"},
             {"side": "carrier", "name": "Ravi", "role": "Finance"},
             {"side": "counterparty", "name": "Xemo", "role": "Broker"}]
    marks = _canvas_marks(three)
    lay = esign_pdf.normalise_signature_layout(
        {"arrangement": "placed",
         "fields": {s: list(esign_pdf.DEFAULT_SIGNATURE_FIELDS)
                    for s in esign_pdf.SIGNATURE_SIDES},
         "blocks": {"carrier": _drop(marks, 1, 0.35, 0.1),
                    "carrier#2": _drop(marks, 1, 0.55, 0.1),
                    "carrier#3": _drop(marks, 1, 0.75, 0.1),
                    "counterparty": {"after": 5}}}, strict=True)
    pdf = _compose_long(lay, signers=three)
    # Every one of them has boxes, and they are their own.
    keys = sorted(f.party_key for f in esign_pdf.discover_fields(pdf)
                  if f.type == "signature")
    assert keys == ["broker:2", "tenant:1", "tenant:1#2", "tenant:1#3"], keys
    # And each name is printed once, in one block, not repeated in all three.
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        whole = "".join(pg.get_text() for pg in doc)
    finally:
        doc.close()
    for name in ("Giri", "Asha", "Ravi"):
        assert whole.count(name) == 1, (name, whole.count(name))


def test_the_canvas_reserves_a_dropped_block_without_drawing_it():
    """The room is made and the box being dragged sits in it. Drawn as well,
    every drop would leave a printed twin behind the box still moving."""
    marks = _canvas_marks()
    lay = esign_pdf.normalise_signature_layout(
        {"arrangement": "placed",
         "fields": {s: list(esign_pdf.DEFAULT_SIGNATURE_FIELDS)
                    for s in esign_pdf.SIGNATURE_SIDES},
         "blocks": {"carrier": _drop(marks, 1, 0.45, 0.1),
                    "counterparty": {"after": 4}}}, strict=True)
    canvas = cw.compose_pdf(
        name="T", carrier_name="Insurisk", counterparty_name="CRC",
        sections=LONG, tokens={}, schedule=[], signers=None,
        signature_layout=lay, anchors=ANCHORS, blank_signature_space=True)
    doc = fitz.open(stream=canvas, filetype="pdf")
    try:
        whole = "".join(pg.get_text() for pg in doc)
    finally:
        doc.close()
    # The tied side IS drawn — it is part of the document being looked at.
    assert whole.count("For the Counterparty") == 1
    # The dropped one is not: its room is reserved and the screen fills it.
    assert whole.count("For the Carrier") == 0


def test_the_canvas_serves_the_marks_a_drop_is_resolved_against(w):
    r = client.post("/contract-wording/pages", headers=w["ch"],
                    json=_wizard_body())
    assert r.status_code == 200, r.text
    marks = r.json()["marks"]
    assert marks, "no marks served"
    # In reading order, and on real pages.
    assert marks == sorted(marks, key=lambda m: (m["page"], m["y"]))
    for m in marks:
        assert m["page"] >= 1 and 0 <= m["y"] <= 1, m


def test_a_drop_that_names_no_mark_at_all_is_refused():
    for bad in ({"at": 0, "gap": 0, "x": 0}, {"at": 1, "gap": 2, "x": 0},
                {"at": 1, "gap": 0, "x": 5}, {"at": "x", "gap": 0, "x": 0}):
        with pytest.raises(esign_pdf.SignatureLayoutError):
            esign_pdf.normalise_signature_layout(
                {"arrangement": "placed",
                 "blocks": {"carrier": bad,
                            "counterparty": {"page": 1, "x": .5, "y": .5}}},
                strict=True)


# ── the page that is only there when it is needed ──────────────────────────
#
# The signature page is Kavachio's — a heading, the sentence the parties sign
# under, and the blocks. A contract whose blocks have all been moved INTO the
# wording has already signed everywhere it is going to, and what is left is a
# heading promising signatures with none underneath it. That reads as a
# document that lost something, so it is not emitted.

SIG_SENTENCE = "Signed for and on behalf"


def _text(pdf):
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        return [pg.get_text() for pg in doc]
    finally:
        doc.close()


def test_no_orphan_signature_page_when_every_block_moved_into_the_wording():
    marks = _canvas_marks()
    lay = esign_pdf.normalise_signature_layout(
        {"arrangement": "placed",
         "fields": {s: list(esign_pdf.DEFAULT_SIGNATURE_FIELDS)
                    for s in esign_pdf.SIGNATURE_SIDES},
         "blocks": {"carrier": _drop(marks, 1, 0.45, 0.1),
                    "counterparty": {"after": 4}}}, strict=True)
    pages = _text(_compose_long(lay))
    assert not any(SIG_SENTENCE in t for t in pages), "orphan signature page"
    # And both sides still sign, where they were put.
    assert sum(t.count("For the Carrier") for t in pages) == 1
    assert sum(t.count("For the Counterparty") for t in pages) == 1


def test_the_signature_page_comes_back_when_a_block_is_taken_off():
    """Taking a block off (×) gives its side back to the signature page. If the
    page did not come back with it, there would be nowhere to sign."""
    marks = _canvas_marks()
    lay = esign_pdf.normalise_signature_layout(
        {"arrangement": "side_by_side",
         "fields": {s: list(esign_pdf.DEFAULT_SIGNATURE_FIELDS)
                    for s in esign_pdf.SIGNATURE_SIDES},
         "blocks": {"carrier": _drop(marks, 1, 0.45, 0.1)}}, strict=True)
    pages = _text(_compose_long(lay))
    assert any(SIG_SENTENCE in t for t in pages)
    assert sum(t.count("For the Counterparty") for t in pages) == 1


def test_a_block_whose_paragraph_was_edited_away_still_has_somewhere_to_sign():
    """The silent failure this guards. Place a block, then cut the wording down
    — the mark it was dropped after no longer exists. Losing the block there
    would leave a contract nobody can sign, and nothing would say so."""
    marks = _canvas_marks()                       # measured on the LONG wording
    gone = {"at": marks[-2]["n"], "gap": 0.05, "x": 0.1}
    lay = esign_pdf.normalise_signature_layout(
        {"arrangement": "placed",
         "fields": {s: list(esign_pdf.DEFAULT_SIGNATURE_FIELDS)
                    for s in esign_pdf.SIGNATURE_SIDES},
         "blocks": {"carrier": gone, "counterparty": gone}}, strict=True)
    short = [{"title": "Only clause", "body": "Short."}]
    pdf = cw.compose_pdf(
        name="T", carrier_name="Insurisk", counterparty_name="CRC",
        sections=short, tokens={}, schedule=[], signers=None,
        signature_layout=lay, anchors=ANCHORS)
    keys = sorted(f.party_key for f in esign_pdf.discover_fields(pdf)
                  if f.type == "signature")
    assert keys == ["broker:2", "tenant:1"], keys
    assert any("For the Carrier" in t for t in _text(pdf))


def test_a_clause_anchor_with_no_clause_left_falls_back_the_same_way():
    for sections in ([{"title": "Only clause", "body": "Short."}], []):
        lay = esign_pdf.normalise_signature_layout(
            {"arrangement": "placed",
             "fields": {s: list(esign_pdf.DEFAULT_SIGNATURE_FIELDS)
                        for s in esign_pdf.SIGNATURE_SIDES},
             "blocks": {"carrier": {"after": 99},
                        "counterparty": {"after": 99}}}, strict=True)
        pdf = cw.compose_pdf(
            name="T", carrier_name="Insurisk", counterparty_name="CRC",
            sections=sections, tokens={}, schedule=[("Schedule", [["a", "b"]])],
            signers=None, signature_layout=lay, anchors=ANCHORS)
        keys = sorted(f.party_key for f in esign_pdf.discover_fields(pdf)
                      if f.type == "signature")
        assert keys == ["broker:2", "tenant:1"], (sections, keys)


def test_dropping_one_persons_block_leaves_the_others_where_they_were():
    """The bug a sweep found. Dropping the SECOND signatory took the first and
    third off the signature page with them, and neither had anywhere to sign:
    the block that carries a side's people is slot 1's, so slot 1 is the only
    one whose move takes it anywhere."""
    three = [{"side": "carrier", "name": "Alphonse"},
             {"side": "carrier", "name": "Bertrand"},
             {"side": "carrier", "name": "Celestine"},
             {"side": "counterparty", "name": "Dorotheus"}]
    marks = _canvas_marks(three)
    lay = esign_pdf.normalise_signature_layout(
        {"arrangement": "side_by_side",
         "fields": {s: list(esign_pdf.DEFAULT_SIGNATURE_FIELDS)
                    for s in esign_pdf.SIGNATURE_SIDES},
         "blocks": {"carrier#2": _drop(marks, 1, 0.55, 0.1)}}, strict=True)
    pdf = _compose_long(lay, signers=three)
    keys = sorted(f.party_key for f in esign_pdf.discover_fields(pdf)
                  if f.type == "signature")
    assert keys == ["broker:2", "tenant:1", "tenant:1#2", "tenant:1#3"], keys
    whole = "".join(_text(pdf))
    for name in ("Alphonse", "Bertrand", "Celestine", "Dorotheus"):
        assert whole.count(name) == 1, (name, whole.count(name))


def test_a_side_dropped_still_carries_the_people_who_were_not():
    """Slot 1's block is everybody's until they are given one of their own."""
    three = [{"side": "carrier", "name": "Alphonse"},
             {"side": "carrier", "name": "Bertrand"},
             {"side": "carrier", "name": "Celestine"},
             {"side": "counterparty", "name": "Dorotheus"}]
    marks = _canvas_marks(three)
    lay = esign_pdf.normalise_signature_layout(
        {"arrangement": "placed",
         "fields": {s: list(esign_pdf.DEFAULT_SIGNATURE_FIELDS)
                    for s in esign_pdf.SIGNATURE_SIDES},
         "blocks": {"carrier": _drop(marks, 1, 0.40, 0.1),
                    "carrier#3": _drop(marks, 2, 0.35, 0.1),
                    "counterparty": {"after": 5}}}, strict=True)
    pdf = _compose_long(lay, signers=three)
    keys = sorted(f.party_key for f in esign_pdf.discover_fields(pdf)
                  if f.type == "signature")
    assert keys == ["broker:2", "tenant:1", "tenant:1#2", "tenant:1#3"], keys
    whole = "".join(_text(pdf))
    # Two carrier blocks: one carrying 1 and 2, one carrying 3.
    assert whole.count("For the Carrier") == 2
    for name in ("Alphonse", "Bertrand", "Celestine"):
        assert whole.count(name) == 1, (name, whole.count(name))


def test_the_screen_is_told_where_a_block_actually_landed():
    """A block dropped near the foot of a page does not fit there, and the
    typesetter carries it to the next one. The box on screen has to follow it,
    or it claims a page the block is not on."""
    marks = _canvas_marks()
    low = [m for m in marks if m["page"] == 2][-1]
    spot = {"at": low["n"], "gap": round(0.90 - low["y"], 5), "x": 0.10}
    lay = esign_pdf.normalise_signature_layout(
        {"arrangement": "placed",
         "fields": {s: list(esign_pdf.DEFAULT_SIGNATURE_FIELDS)
                    for s in esign_pdf.SIGNATURE_SIDES},
         "blocks": {"carrier": spot, "counterparty": {"after": 5}}}, strict=True)
    landed: dict = {}
    cw.compose_pdf(name="T", carrier_name="Insurisk", counterparty_name="CRC",
                   sections=LONG, tokens={}, schedule=[], signers=None,
                   signature_layout=lay, anchors=ANCHORS,
                   blank_signature_space=True, landings_out=landed)
    assert "carrier" in landed, landed
    # It could not fit at y=0.90, so it is NOT reported there.
    assert landed["carrier"]["page"] >= 2
    assert not (landed["carrier"]["page"] == 2
                and landed["carrier"]["y"] == pytest.approx(0.90, abs=0.01))
    # And the real document agrees with what the screen was told.
    pdf = _compose_long(lay)
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        pg = next(i for i, p in enumerate(doc, 1)
                  if "For the Carrier" in p.get_text())
    finally:
        doc.close()
    assert pg == landed["carrier"]["page"], (pg, landed)


def test_a_landing_is_reported_for_every_dropped_block(w):
    r = client.post("/contract-wording/pages", headers=w["ch"],
                    json=_wizard_body())
    assert r.status_code == 200, r.text
    assert "landings" in r.json()
    assert r.json()["landings"] == {}, "nothing placed, nothing to land"


# ── placed by hand means BY HAND ───────────────────────────────────────────
#
# Choosing it is saying "I will say where these go". A page Kavachio adds
# anyway — a heading, the sentence, and the room underneath — is the screen not
# taking that answer, so it goes from the moment the choice is made rather than
# once the last block has been dragged. Half-placed is a state somebody passes
# through, and a page that appears and vanishes underneath them while they work
# is worse than either answer.

def _canvas(layout, sections=None):
    pdf = cw.compose_pdf(
        name="T", carrier_name="Insurisk", counterparty_name="CRC",
        sections=sections if sections is not None else LONG,
        tokens={}, schedule=[], signers=None, signature_layout=layout,
        anchors=ANCHORS, blank_signature_space=True)
    return "".join(_text(pdf))


def test_choosing_placed_by_hand_takes_the_signature_page_away_at_once():
    """Before anything has been dragged. This is the state somebody is in the
    instant they pick the option, and it is the one they look at."""
    asked = {"arrangement": "placed",
             "fields": {s: list(esign_pdf.DEFAULT_SIGNATURE_FIELDS)
                        for s in esign_pdf.SIGNATURE_SIDES},
             "blocks": {}}
    assert SIG_SENTENCE not in _canvas(asked)


def test_the_automatic_arrangements_keep_their_signature_page():
    for arr in ("side_by_side", "stacked"):
        lay = {"arrangement": arr,
               "fields": {s: list(esign_pdf.DEFAULT_SIGNATURE_FIELDS)
                          for s in esign_pdf.SIGNATURE_SIDES}}
        assert SIG_SENTENCE in _canvas(lay), arr


def test_a_hand_placed_contract_has_no_signature_page_in_the_real_document():
    marks = _canvas_marks()
    lay = esign_pdf.normalise_signature_layout(
        {"arrangement": "placed",
         "fields": {s: list(esign_pdf.DEFAULT_SIGNATURE_FIELDS)
                    for s in esign_pdf.SIGNATURE_SIDES},
         "blocks": {"carrier": _drop(marks, 1, 0.45, 0.10),
                    "counterparty": _drop(marks, 2, 0.40, 0.55)}}, strict=True)
    pdf = _compose_long(lay)
    assert SIG_SENTENCE not in "".join(_text(pdf))
    keys = sorted(f.party_key for f in esign_pdf.discover_fields(pdf)
                  if f.type == "signature")
    assert keys == ["broker:2", "tenant:1"], keys


def test_but_a_stored_half_placed_layout_still_gets_one(w):
    """The read path never leaves a contract unsignable. A row that says
    "placed" and placed nothing is a row somebody abandoned mid-choice, and it
    still has to reach a signer with somewhere to sign."""
    for blocks in ({}, {"carrier": {"at": 2, "gap": 0.02, "x": 0.1}}):
        pdf = _compose_long({"arrangement": "placed",
                             "fields": {s: list(esign_pdf.DEFAULT_SIGNATURE_FIELDS)
                                        for s in esign_pdf.SIGNATURE_SIDES},
                             "blocks": blocks})
        whole = "".join(_text(pdf))
        assert SIG_SENTENCE in whole, blocks
        keys = sorted(f.party_key for f in esign_pdf.discover_fields(pdf)
                      if f.type == "signature")
        assert keys == ["broker:2", "tenant:1"], (blocks, keys)
