"""Create-a-Contract step 4 — the signing round, end to end.

The rule these tests exist for is the one the whole design turns on:

    a signature box belongs to exactly ONE organisation, named on the box as
    `field_party_key` — 'tenant:<tenant_id>' for the insurer, 'broker:<id>' for
    the broker — and nobody else can write to it.

Everything else here (the ordering, the hand-off, the spent link) protects the
same thing: that at the end there is ONE document, and every signature on it was
put there by the party whose block it is. A regression in any of them is a
contract signed by the wrong person, which is not a bug you find in production.

Sends no mail: MAIL_ALLOWED_RECIPIENTS is set to a dead address before the app
is imported, so every send is skipped-and-logged after the body is built — the
templates are still exercised, nothing leaves the machine.
"""
import os

# Must precede the app import: email_utils reads this at call time, but the
# guard has to be in place before any test can trigger a send.
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

# Every code the server generates during a test, newest last.
#
# Captured by WRAPPING the real generator rather than replacing it, so the tests
# exercise the same CSPRNG, the same length and the same bcrypt hashing that
# production does — a fixed "123456" would quietly stop testing all three.
ISSUED_CODES: list[str] = []


@pytest.fixture(autouse=True)
def _capture_codes(monkeypatch):
    import esign_otp
    real = esign_otp.generate_code

    def spy():
        code = real()
        ISSUED_CODES.append(code)
        return code

    monkeypatch.setattr(esign_otp, "generate_code", spy)
    ISSUED_CODES.clear()
    yield

TINY_PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
            "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


@pytest.fixture(scope="module")
def world():
    """A carrier, a broker, and one admin on each side."""
    suffix = os.urandom(4).hex()
    with SessionLocal() as s:
        carrier = Tenant(tenant_name=f"esign-{suffix}", legal_name="Insurisk Specialty")
        s.add(carrier); s.commit()
        broker = Party(tenant_id=carrier.id, party_type="broker",
                       legal_name="CRC Insurisk", reference=f"esign-b-{suffix}")
        s.add(broker); s.commit()
        prog = Program(tenant_id=carrier.id, name="Spectrum Transportation",
                       is_app_managed=True)
        s.add(prog); s.commit()
        s.add(ProgramBroker(tenant_id=carrier.id, program_id=prog.id,
                            broker_party_id=broker.id, status="active"))
        # enforce_invitation_chain(): every login but the platform's first must
        # record who let it in. A carrier admin is invited by Kavachio; a broker
        # admin by the carrier — which is the real chain, not a test detail.
        founder = (s.query(AppUser)
                   .filter(AppUser.role == "kavachio_admin")
                   .order_by(AppUser.id).first())
        if founder is None:
            pytest.skip("no kavachio_admin on this database to seed an invite chain")
        dana = AppUser(tenant_id=carrier.id, email=f"dana-{suffix}@insurisk.test",
                       full_name="Dana Alvarez", role="carrier_admin",
                       invited_by_user_id=founder.id)
        s.add(dana); s.commit()
        marco = AppUser(tenant_id=None, broker_party_id=broker.id,
                        email=f"marco-{suffix}@crc.test", full_name="Marco Vance",
                        role="broker_admin", invited_by_user_id=dana.id)
        s.add(marco); s.commit()
        return {
            "tenant": carrier.id, "broker": broker.id, "program": prog.id,
            "carrier_admin": dana.id,
            "headers": {"Authorization":
                        f"Bearer {mint_access_token(dana.id, carrier.id, 'carrier_admin')}"},
        }


def _create(world, **over):
    body = {"title": "Schedule A — 2027", "broker_party_id": world["broker"],
            "program_id": world["program"], "source": "sample", "send_now": False}
    body.update(over)
    r = client.post("/esign/envelopes", headers=world["headers"], json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _token(env, side):
    rec = next(x for x in env["recipients"] if x["side"] == side)
    return (rec.get("link") or "").split("token=")[-1] or None


def _unlock(token, code=None):
    """Type the one-time code and return the header every later call carries.

    Every signing test goes through here, which is the point: if the gate ever
    stops being enforced, these fail rather than silently passing on a link
    alone."""
    r = client.post(f"/esign/sign/{token}/verify",
                    json={"code": code or ISSUED_CODES[-1]})
    assert r.status_code == 200, r.text
    return {"X-Esign-Session": r.json()["session"]}


def _open(token, headers):
    r = client.get(f"/esign/sign/{token}", headers=headers)
    assert r.status_code == 200, r.text
    v = r.json()
    assert v.get("locked") is False, "expected an unlocked view"
    return v


def _initials(name: str) -> str:
    return "".join(w[0] for w in name.split() if w).upper()


def _fill(view, name, title):
    """The values a signer would type, for their own boxes only.

    An INITIALS box gets initials, not the full name — the browser fills it that
    way and the two marks are meant to be different. See
    test_initials_are_a_second_mark_not_the_signature_again."""
    today = "07 Sep 2026"
    out = []
    for f in view["fields"]:
        if not f["mine"]:
            continue
        out.append({"field_id": f["id"], "value": {
            "signature": name, "initial": _initials(name), "name": name,
            "title": title, "date": today}.get(f["type"], "")})
    return out


# ── setting a round up ──────────────────────────────────────────────────────
def test_boxes_are_owned_by_the_two_organisations(world):
    env = _create(world)
    assert env["status"] == "draft", "nothing may be sent until Send is pressed"
    keys = {r["side"]: r["party_key"] for r in env["recipients"]}
    assert keys["insurer"] == f"tenant:{world['tenant']}"
    assert keys["broker"] == f"broker:{world['broker']}"
    # Every box on the document names one of exactly those two, and both get some.
    owners = {f["party_key"] for f in env["fields"]}
    assert owners == set(keys.values()), owners
    assert env["fields"], "the sample contract must carry signature anchors"


def test_the_broker_signer_is_resolved_from_the_broker_party(world):
    env = _create(world)
    brk = next(r for r in env["recipients"] if r["side"] == "broker")
    assert brk["broker_party_id"] == world["broker"]
    assert brk["tenant_id"] is None, "a broker seat never carries a carrier tenant"
    ins = next(r for r in env["recipients"] if r["side"] == "insurer")
    assert ins["tenant_id"] == world["tenant"]
    assert ins["broker_party_id"] is None


# ── sending, in order ───────────────────────────────────────────────────────
def test_only_the_first_signer_is_given_a_link(world):
    env = _create(world)
    sent = client.post(f"/esign/envelopes/{env['id']}/send",
                       headers=world["headers"], json={}).json()
    live = [r for r in sent["recipients"] if r.get("link")]
    assert len(live) == 1 and live[0]["side"] == "insurer", (
        "the broker must not be reachable until the insurer has signed — two "
        "live links is how two people sign two different documents")
    assert sent["waiting_on"]["side"] == "insurer"


def test_a_signer_sees_the_whole_document_but_owns_only_their_boxes(world):
    env = _create(world)
    sent = client.post(f"/esign/envelopes/{env['id']}/send",
                       headers=world["headers"], json={}).json()
    tok = _token(sent, "insurer")
    view = _open(tok, _unlock(tok))
    assert view["me"]["party_key"] == f"tenant:{world['tenant']}"
    assert view["me"]["my_turn"] is True
    mine = [f for f in view["fields"] if f["mine"]]
    theirs = [f for f in view["fields"] if not f["mine"]]
    assert mine and theirs, "both sides' blocks must be visible"
    assert all(f["party_key"] == view["me"]["party_key"] for f in mine)
    assert all(f["party_key"] != view["me"]["party_key"] for f in theirs)
    # A locked box says whose it is — a signer who cannot tell gives up.
    assert all(f["owner_org"] for f in theirs)


# ── THE rule ────────────────────────────────────────────────────────────────
def test_a_signer_cannot_fill_the_other_sides_box(world):
    env = _create(world)
    sent = client.post(f"/esign/envelopes/{env['id']}/send",
                       headers=world["headers"], json={}).json()
    tok = _token(sent, "insurer")
    h = _unlock(tok)
    view = _open(tok, h)
    theirs = next(f for f in view["fields"] if not f["mine"])
    r = client.post(f"/esign/sign/{tok}", headers=h, json={
        "agreed": True, "signature_name": "Dana Alvarez",
        "fields": [{"field_id": theirs["id"], "value": "Dana Alvarez"}]})
    assert r.status_code == 403, r.text
    # And nothing was written on the way to refusing.
    again = _open(tok, h)
    assert not next(f for f in again["fields"] if f["id"] == theirs["id"])["filled"]


def test_signing_requires_the_electronic_signature_consent(world):
    env = _create(world)
    sent = client.post(f"/esign/envelopes/{env['id']}/send",
                       headers=world["headers"], json={}).json()
    tok = _token(sent, "insurer")
    h = _unlock(tok)
    view = _open(tok, h)
    r = client.post(f"/esign/sign/{tok}", headers=h, json={
        "agreed": False, "signature_name": "Dana Alvarez",
        "fields": _fill(view, "Dana Alvarez", "Carrier Admin")})
    assert r.status_code == 400


# ── the hand-off, which is the point of the whole feature ───────────────────
def test_the_broker_gets_the_document_the_insurer_signed(world):
    env = _create(world)
    sent = client.post(f"/esign/envelopes/{env['id']}/send",
                       headers=world["headers"], json={}).json()
    ins_tok = _token(sent, "insurer")
    ins_h = _unlock(ins_tok)
    ins_view = _open(ins_tok, ins_h)
    r = client.post(f"/esign/sign/{ins_tok}", headers=ins_h, json={
        "agreed": True, "signature_name": "Dana Alvarez",
        "fields": _fill(ins_view, "Dana Alvarez", "Carrier Admin")})
    assert r.status_code == 200 and r.json()["status"] == "in_progress", r.text

    # The insurer's link is spent the moment it has been used.
    assert client.get(f"/esign/sign/{ins_tok}", headers=ins_h).status_code == 404

    full = client.get(f"/esign/envelopes/{env['id']}", headers=world["headers"]).json()
    brk_tok = _token(full, "broker")
    assert brk_tok, "signing must issue the next signer's link"

    brk_h = _unlock(brk_tok)          # the hand-off email carries its own code
    brk = _open(brk_tok, brk_h)
    assert brk["me"]["party_key"] == f"broker:{world['broker']}"
    assert len(brk["already_signed"]) == 1
    locked = [f for f in brk["fields"] if not f["mine"]]
    assert locked and all(f["filled"] for f in locked), (
        "the broker must open the document carrying the insurer's signature, "
        "not a blank copy of it")
    # Same document, further on: the stamped version has a higher pdf_version.
    assert brk["envelope"]["pdf_version"] > 1
    assert client.get(f"/esign/sign/{brk_tok}/pages/"
                      f"{brk['envelope']['page_count']}",
                      headers=brk_h).status_code == 200


def test_the_broker_cannot_sign_the_insurers_block_either(world):
    env = _create(world)
    sent = client.post(f"/esign/envelopes/{env['id']}/send",
                       headers=world["headers"], json={}).json()
    ins_tok = _token(sent, "insurer")
    ins_h = _unlock(ins_tok)
    v = _open(ins_tok, ins_h)
    client.post(f"/esign/sign/{ins_tok}", headers=ins_h, json={
        "agreed": True, "signature_name": "Dana Alvarez",
        "fields": _fill(v, "Dana Alvarez", "Carrier Admin")})
    full = client.get(f"/esign/envelopes/{env['id']}", headers=world["headers"]).json()
    brk_tok = _token(full, "broker")
    brk_h = _unlock(brk_tok)
    brk = _open(brk_tok, brk_h)
    ins_sig = next(f for f in brk["fields"]
                   if not f["mine"] and f["type"] == "signature")
    r = client.post(f"/esign/sign/{brk_tok}", headers=brk_h, json={
        "agreed": True, "signature_name": "Marco Vance",
        "fields": [{"field_id": ins_sig["id"], "value": "Marco Vance"}]})
    assert r.status_code == 403, r.text


def test_the_round_completes_and_both_links_die(world):
    env = _create(world)
    sent = client.post(f"/esign/envelopes/{env['id']}/send",
                       headers=world["headers"], json={}).json()
    ins_tok = _token(sent, "insurer")
    ins_h = _unlock(ins_tok)
    v = _open(ins_tok, ins_h)
    client.post(f"/esign/sign/{ins_tok}", headers=ins_h, json={
        "agreed": True, "signature_name": "Dana Alvarez",
        "fields": _fill(v, "Dana Alvarez", "Carrier Admin")})

    full = client.get(f"/esign/envelopes/{env['id']}", headers=world["headers"]).json()
    brk_tok = _token(full, "broker")
    brk_h = _unlock(brk_tok)
    bv = _open(brk_tok, brk_h)
    pages_before = bv["envelope"]["page_count"]
    r = client.post(f"/esign/sign/{brk_tok}", headers=brk_h, json={
        "agreed": True, "signature_name": "Marco Vance",
        "signature_image": TINY_PNG,
        "fields": _fill(bv, "Marco Vance", "Broker Admin")})
    assert r.status_code == 200 and r.json()["status"] == "completed", r.text

    done = client.get(f"/esign/envelopes/{env['id']}", headers=world["headers"]).json()
    assert done["status"] == "completed"
    assert done["page_count"] == pages_before, (
        "the delivered file is the contract and nothing else — completing it "
        "must not staple an audit page onto the back")
    assert all(x["status"] == "signed" for x in done["recipients"])
    for tok, h in ((ins_tok, ins_h), (brk_tok, brk_h)):
        assert client.get(f"/esign/sign/{tok}", headers=h).status_code == 404

    pdf = client.get(f"/esign/envelopes/{env['id']}/pdf", headers=world["headers"])
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    # An em dash in the title must not break the latin-1 header.
    assert "filename*=UTF-8''" in pdf.headers["content-disposition"]


# ── declining, and withdrawing ──────────────────────────────────────────────
def test_declining_stops_the_round_and_carries_the_reason(world):
    env = _create(world)
    sent = client.post(f"/esign/envelopes/{env['id']}/send",
                       headers=world["headers"], json={}).json()
    tok = _token(sent, "insurer")
    h = _unlock(tok)
    assert client.post(f"/esign/sign/{tok}/decline", headers=h,
                       json={"reason": "no"}).status_code == 400   # too short
    r = client.post(f"/esign/sign/{tok}/decline", headers=h,
                    json={"reason": "Commission should be 12.5%, not 15%."})
    assert r.status_code == 200
    out = client.get(f"/esign/envelopes/{env['id']}", headers=world["headers"]).json()
    assert out["status"] == "declined"
    assert "12.5" in next(x for x in out["recipients"]
                          if x["side"] == "insurer")["decline_reason"]
    assert client.get(f"/esign/sign/{tok}", headers=h).status_code == 404


def test_withdrawing_kills_every_outstanding_link(world):
    env = _create(world)
    sent = client.post(f"/esign/envelopes/{env['id']}/send",
                       headers=world["headers"], json={}).json()
    tok = _token(sent, "insurer")
    h = _unlock(tok)
    assert client.get(f"/esign/sign/{tok}", headers=h).status_code == 200
    assert client.post(f"/esign/envelopes/{env['id']}/void",
                       headers=world["headers"]).status_code == 200
    assert client.get(f"/esign/sign/{tok}", headers=h).status_code == 404, (
        "withdrawing must stop the link opening, not just mark a row")


# ── tenant isolation ────────────────────────────────────────────────────────
def test_another_carrier_cannot_see_or_touch_the_envelope(world):
    env = _create(world)
    with SessionLocal() as s:
        other = Tenant(tenant_name=f"other-{os.urandom(4).hex()}", legal_name="Northgate")
        s.add(other); s.commit()
        founder = (s.query(AppUser).filter(AppUser.role == "kavachio_admin")
                   .order_by(AppUser.id).first())
        u = AppUser(tenant_id=other.id, email=f"x-{os.urandom(4).hex()}@n.test",
                    full_name="Other Admin", role="carrier_admin",
                    invited_by_user_id=(founder.id if founder else None))
        s.add(u); s.commit()
        h = {"Authorization": f"Bearer {mint_access_token(u.id, other.id, 'carrier_admin')}"}
    # 404 rather than 403: an id another carrier holds must not be confirmable.
    assert client.get(f"/esign/envelopes/{env['id']}", headers=h).status_code == 404
    assert client.post(f"/esign/envelopes/{env['id']}/send",
                       headers=h, json={}).status_code == 404
    assert env["id"] not in [e["id"] for e in
                             client.get("/esign/envelopes", headers=h).json()["envelopes"]]


# ── the one-time code ───────────────────────────────────────────────────────
# The link used to BE the credential. These tests are the reason it no longer is.
def _sent_envelope(world):
    env = _create(world)
    return client.post(f"/esign/envelopes/{env['id']}/send",
                       headers=world["headers"], json={}).json()


def test_the_link_alone_reveals_nothing_about_the_contract(world):
    """A URL leaks through history, chat, screen shares and forwarded mail. What
    it buys you now is a lock screen and nothing else — not the title, not the
    parties, not the page count, and above all not the document."""
    sent = _sent_envelope(world)
    tok = _token(sent, "insurer")
    v = client.get(f"/esign/sign/{tok}").json()
    assert v["locked"] is True
    for leak in ("fields", "envelope", "me", "others", "already_signed"):
        assert leak not in v, f"the lock screen leaked {leak}"
    # Enough to know which inbox to look in, not enough to learn an address.
    assert "@" in v["email_hint"] and "*" in v["email_hint"]
    assert v["email_hint"] != next(
        r["email"] for r in sent["recipients"] if r["side"] == "insurer")


def test_the_document_itself_is_behind_the_code(world):
    """Not just the JSON. A page image IS the contract, and the PDF is all of
    it — serving either to a locked link would make the code decorative."""
    sent = _sent_envelope(world)
    tok = _token(sent, "insurer")
    assert client.get(f"/esign/sign/{tok}/pages/1").status_code == 401
    assert client.get(f"/esign/sign/{tok}/pdf").status_code == 401
    v = _open(tok, _unlock(tok))
    assert client.post(f"/esign/sign/{tok}", json={
        "agreed": True, "signature_name": "Dana Alvarez",
        "fields": _fill(v, "Dana Alvarez", "Carrier Admin")}).status_code == 401, (
        "signing without the code must be refused")


def test_the_right_code_opens_it(world):
    sent = _sent_envelope(world)
    tok = _token(sent, "insurer")
    r = client.post(f"/esign/sign/{tok}/verify", json={"code": ISSUED_CODES[-1]})
    assert r.status_code == 200 and r.json()["session"]
    h = {"X-Esign-Session": r.json()["session"]}
    assert _open(tok, h)["me"]["party_key"] == f"tenant:{world['tenant']}"
    # Page images cannot set headers, so the same session works in the query.
    assert client.get(f"/esign/sign/{tok}/pages/1",
                      params={"session": r.json()["session"]}).status_code == 200


def test_a_wrong_code_is_refused_and_counted_down(world):
    sent = _sent_envelope(world)
    tok = _token(sent, "insurer")
    wrong = "000000" if ISSUED_CODES[-1] != "000000" else "111111"
    r = client.post(f"/esign/sign/{tok}/verify", json={"code": wrong})
    assert r.status_code == 400
    # The person who mistyped needs to know they are running out; an attacker
    # learns nothing five requests would not have told them.
    assert "left" in r.json()["detail"]
    assert client.get(f"/esign/sign/{tok}").json()["attempts_left"] == 4


def test_five_wrong_codes_lock_the_link_even_against_the_right_one(world):
    """Six digits is a million guesses — trivial for a script if you let it try.
    The lockout is what makes the code worth anything, so the RIGHT code must
    also be refused while it holds."""
    sent = _sent_envelope(world)
    tok = _token(sent, "insurer")
    right = ISSUED_CODES[-1]
    wrong = "000000" if right != "000000" else "111111"
    codes = [client.post(f"/esign/sign/{tok}/verify",
                         json={"code": wrong}).status_code for _ in range(5)]
    assert codes[:4] == [400, 400, 400, 400], codes
    assert codes[4] == 429, "the fifth wrong answer must lock it"
    r = client.post(f"/esign/sign/{tok}/verify", json={"code": right})
    assert r.status_code == 429, "a lockout that the right code walks through is not a lockout"
    v = client.get(f"/esign/sign/{tok}").json()
    assert v["lockout_seconds"] > 0 and v["attempts_left"] == 0


def test_a_session_does_not_work_on_another_signers_link(world):
    """Both signers hold a link to the same envelope. Unlocking one must not
    unlock the other, or the code is per-contract rather than per-person."""
    env = _create(world)
    sent = client.post(f"/esign/envelopes/{env['id']}/send",
                       headers=world["headers"], json={}).json()
    ins_tok = _token(sent, "insurer")
    ins_h = _unlock(ins_tok)
    v = _open(ins_tok, ins_h)
    client.post(f"/esign/sign/{ins_tok}", headers=ins_h, json={
        "agreed": True, "signature_name": "Dana Alvarez",
        "fields": _fill(v, "Dana Alvarez", "Carrier Admin")})
    full = client.get(f"/esign/envelopes/{env['id']}", headers=world["headers"]).json()
    brk_tok = _token(full, "broker")
    # The insurer's session, pointed at the broker's link.
    assert client.get(f"/esign/sign/{brk_tok}", headers=ins_h).json()["locked"] is True
    assert client.get(f"/esign/sign/{brk_tok}/pages/1",
                      headers=ins_h).status_code == 401


def test_a_garbage_session_is_just_locked_not_accepted(world):
    sent = _sent_envelope(world)
    tok = _token(sent, "insurer")
    for junk in ("", "not-a-jwt", "a.b.c"):
        v = client.get(f"/esign/sign/{tok}", headers={"X-Esign-Session": junk}).json()
        assert v["locked"] is True, junk


def test_the_code_is_never_stored_in_plain_text(world):
    """A readable signing code is a readable signing code to anyone with a
    database backup."""
    from sqlalchemy import text as sql
    sent = _sent_envelope(world)
    code = ISSUED_CODES[-1]
    with SessionLocal() as s:
        rows = s.execute(sql(
            "SELECT recipient_otp_hash FROM contract_esign_recipient "
            "WHERE recipient_envelope_id = :e"), {"e": sent["id"]}).fetchall()
    stored = [r[0] for r in rows if r[0]]
    assert stored, "a code should have been issued"
    for h in stored:
        assert code not in h
        assert h.startswith("$2b$"), f"expected bcrypt, got {h[:8]}"


def _age_last_send(envelope_id: int, seconds: int = 300) -> None:
    """Pretend the last code went out `seconds` ago.

    Sending the envelope emails a code, so a resend one second later is refused
    by the cooldown — correctly. Ageing the timestamp is how the test reaches
    the behaviour on the far side of it WITHOUT turning the cooldown down, which
    would stop testing the thing that protects the signer's inbox."""
    from sqlalchemy import text as sql
    with SessionLocal() as s:
        s.execute(sql("UPDATE contract_esign_recipient "
                      "SET recipient_otp_last_sent_at = now() - make_interval(secs => :n) "
                      "WHERE recipient_envelope_id = :e"),
                  {"n": seconds, "e": envelope_id})
        s.commit()


def test_a_code_cannot_be_resent_immediately(world):
    """The cooldown is what stops the resend button being turned into a way of
    mailing somebody once a second."""
    sent = _sent_envelope(world)
    tok = _token(sent, "insurer")
    r = client.post(f"/esign/sign/{tok}/resend-code")
    assert r.status_code == 429 and "Wait" in r.json()["detail"]


def test_a_fresh_code_can_be_emailed_but_not_endlessly(world):
    sent = _sent_envelope(world)
    tok = _token(sent, "insurer")
    first = ISSUED_CODES[-1]
    _age_last_send(sent["id"])
    r = client.post(f"/esign/sign/{tok}/resend-code")
    assert r.status_code == 200 and "*" in r.json()["sent_to"]
    assert ISSUED_CODES[-1] != first, "a resend must issue a NEW code"
    # The old one stops working; the new one opens it.
    assert client.post(f"/esign/sign/{tok}/verify",
                       json={"code": first}).status_code == 400
    assert client.post(f"/esign/sign/{tok}/verify",
                       json={"code": ISSUED_CODES[-1]}).status_code == 200
    # And the button cannot be leaned on to mail somebody repeatedly.
    assert client.post(f"/esign/sign/{tok}/resend-code").status_code == 429


def test_signing_retires_the_code_with_the_link(world):
    sent = _sent_envelope(world)
    tok = _token(sent, "insurer")
    h = _unlock(tok)
    v = _open(tok, h)
    code = ISSUED_CODES[-1]
    client.post(f"/esign/sign/{tok}", headers=h, json={
        "agreed": True, "signature_name": "Dana Alvarez",
        "fields": _fill(v, "Dana Alvarez", "Carrier Admin")})
    # Neither the session nor the code re-opens a signed contract.
    assert client.get(f"/esign/sign/{tok}", headers=h).status_code == 404
    assert client.post(f"/esign/sign/{tok}/verify",
                       json={"code": code}).status_code == 404


def test_every_signing_email_carries_the_code(world, monkeypatch):
    """The code has to be IN the message, or the signer has no way to get it."""
    bodies = []
    import email_utils
    monkeypatch.setattr(email_utils, "send_email",
                        lambda to, subject, html, **kw: bodies.append(html))
    env = _create(world)
    sent = client.post(f"/esign/envelopes/{env['id']}/send",
                       headers=world["headers"], json={}).json()
    spaced = " ".join(ISSUED_CODES[-1])
    assert any(spaced in b for b in bodies), "the request email must show the code"

    # And the hand-off to the broker carries its own, different code.
    tok = _token(sent, "insurer")
    h = _unlock(tok)
    v = _open(tok, h)
    bodies.clear()
    client.post(f"/esign/sign/{tok}", headers=h, json={
        "agreed": True, "signature_name": "Dana Alvarez",
        "fields": _fill(v, "Dana Alvarez", "Carrier Admin")})
    assert any(" ".join(ISSUED_CODES[-1]) in b for b in bodies)


# ── which mailbox it goes out from ──────────────────────────────────────────
def test_every_signing_email_is_sent_from_the_kavachio_mailbox(world, monkeypatch):
    """Signing mail must go out as the NOTIFY account (dinesh@kavachio.com),
    never the default SMTP_* sender, which is a different company's address.

    This is the first thing a broker's signer ever sees from the platform, and
    the From line is most of what tells them it is genuine — so the account is
    asserted rather than left to whichever sender happens to be configured.
    """
    sent = []
    import email_utils
    monkeypatch.setattr(email_utils, "send_email",
                        lambda *a, **kw: sent.append(kw.get("account")))

    env = _create(world)
    client.post(f"/esign/envelopes/{env['id']}/send",
                headers=world["headers"], json={})
    assert sent, "sending an envelope must email the first signer"
    assert all(acct == "NOTIFY" for acct in sent), sent

    # And on the hand-off and the completed copy, not just the first request.
    sent.clear()
    real = client.post(f"/esign/envelopes/{env['id']}/send",
                       headers=world["headers"], json={}).json()
    tok = _token(real, "insurer")
    h = _unlock(tok)
    v = _open(tok, h)
    client.post(f"/esign/sign/{tok}", headers=h, json={
        "agreed": True, "signature_name": "Dana Alvarez",
        "fields": _fill(v, "Dana Alvarez", "Carrier Admin")})
    assert sent and all(acct == "NOTIFY" for acct in sent), sent


def test_the_notify_account_resolves_to_a_kavachio_address():
    """A configuration test, not a behaviour one: NOTIFY_SMTP_* must actually be
    set, or send_email silently falls back to the default mailbox and the whole
    point of the test above is lost."""
    from email_utils import mail_account
    from notifications import NOTIFY_MAIL_ACCOUNT
    acct = mail_account(NOTIFY_MAIL_ACCOUNT)
    if not acct.user:
        pytest.skip("no SMTP configured in this environment")
    assert acct.sender.endswith("@kavachio.com"), (
        f"signing mail would go out as {acct.sender} — set NOTIFY_SMTP_USER / "
        "NOTIFY_SMTP_PASS / NOTIFY_SMTP_FROM")


# ── taking the wording from steps 1–3 ───────────────────────────────────────
# Migration 14_contract_documents.sql moved the wording off contract.blob and
# onto contract_document, and migration 16 has step 2 composing it as a .docx.
# Both of those are upstream of this feature, and both would silently break the
# "sign the real contract" path if step 4 kept reading the old place.
def _wording_docx(tenant_id: int, broker_id: int) -> bytes:
    """A .docx shaped like the one step 2 composes: wording, then an execution
    page carrying an anchor for each party."""
    import io
    import docx
    d = docx.Document()
    d.add_heading("Binding Authority Agreement", 0)
    d.add_paragraph("Schedule A — 2027.")
    d.add_heading("Signatures", 1)
    d.add_paragraph("For and on behalf of the Insurer")
    d.add_paragraph("{{signature:tenant:%d}}" % tenant_id)
    d.add_paragraph("{{name:tenant:%d}}   {{date:tenant:%d}}" % (tenant_id, tenant_id))
    d.add_paragraph("For and on behalf of the Broker")
    d.add_paragraph("{{signature:broker:%d}}" % broker_id)
    d.add_paragraph("{{name:broker:%d}}   {{date:broker:%d}}" % (broker_id, broker_id))
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _contract_with_wording(world, data: bytes | None, filename: str,
                           doc_type: str = "contract") -> int:
    from sqlalchemy import text as sql
    with SessionLocal() as s:
        c = Contract(tenant_id=world["tenant"], program_id=world["program"],
                     broker_party_id=world["broker"], filename=None,
                     status="drafted", schedule_key="Schedule A — 2027")
        s.add(c); s.commit()
        if data is not None:
            s.execute(sql("""
                INSERT INTO contract_document
                    (contract_document_contract_id, contract_document_type,
                     contract_document_filename, contract_document_blob,
                     contract_document_is_active, tenant_id)
                VALUES (:cid, :type, :name, :blob, TRUE, :tid)
            """), {"cid": c.id, "type": doc_type, "name": filename,
                   "blob": data, "tid": world["tenant"]})
            s.commit()
        return c.id


def test_the_wording_is_taken_from_contract_document_and_converted(world):
    """The whole seam with steps 1–3, in one test: a .docx wording, attached as
    a contract_document row, with nothing on contract.blob at all."""
    cid = _contract_with_wording(
        world, _wording_docx(world["tenant"], world["broker"]), "Schedule_A.docx")
    env = _create(world, contract_id=cid, source="contract")
    assert env["page_count"] >= 1, "the .docx must have been converted to a PDF"
    keys = {f["party_key"] for f in env["fields"]}
    assert keys == {f"tenant:{world['tenant']}", f"broker:{world['broker']}"}, keys
    # Signature, name and date for each side — read out of the Word document.
    assert len([f for f in env["fields"] if f["type"] == "signature"]) == 2
    # And it renders, which is what the signing screen needs.
    r = client.get(f"/esign/envelopes/{env['id']}/pages/1", headers=world["headers"])
    assert r.status_code == 200 and r.content[:8].startswith(b"\x89PNG")


def test_a_reference_document_is_never_mistaken_for_the_wording(world):
    """A 'reference' row is a document the wording QUOTES. Sending one for
    signature would put both parties' names on somebody else's document."""
    cid = _contract_with_wording(
        world, _wording_docx(world["tenant"], world["broker"]),
        "Purchasing_Guidelines.docx", doc_type="reference")
    r = client.post("/esign/envelopes", headers=world["headers"], json={
        "title": "Schedule A — 2027", "broker_party_id": world["broker"],
        "program_id": world["program"], "contract_id": cid,
        "source": "contract", "send_now": False})
    assert r.status_code == 400
    assert "no wording stored" in r.json()["detail"]


def test_a_wording_with_no_anchors_is_refused_with_the_tokens_it_needs(world):
    import io
    import docx
    d = docx.Document()
    d.add_paragraph("A contract with no execution page.")
    buf = io.BytesIO(); d.save(buf)
    cid = _contract_with_wording(world, buf.getvalue(), "No_Blocks.docx")
    r = client.post("/esign/envelopes", headers=world["headers"], json={
        "title": "Schedule A — 2027", "broker_party_id": world["broker"],
        "program_id": world["program"], "contract_id": cid,
        "source": "contract", "send_now": False})
    assert r.status_code == 400
    detail = r.json()["detail"]
    # The message has to name the exact tokens, or whoever owns step 2 is left
    # guessing at a format nothing documents to them.
    assert f"{{{{signature:tenant:{world['tenant']}}}}}" in detail, detail
    assert f"{{{{signature:broker:{world['broker']}}}}}" in detail, detail


def test_a_contract_with_no_wording_anywhere_says_so(world):
    cid = _contract_with_wording(world, None, "")
    r = client.post("/esign/envelopes", headers=world["headers"], json={
        "title": "Schedule A — 2027", "broker_party_id": world["broker"],
        "program_id": world["program"], "contract_id": cid,
        "source": "contract", "send_now": False})
    assert r.status_code == 400 and "no wording stored" in r.json()["detail"]


def test_a_bad_token_is_indistinguishable_from_an_expired_one():
    for tok in ("x" * 40, "not-a-token", ""):
        r = client.get(f"/esign/sign/{tok}")
        assert r.status_code in (404, 405), tok


# ── previews must not masquerade as contracts in flight ─────────────────────
# The carrier reported "two new entries appear when I send one contract". They
# were the review screen's previews: it persists an envelope so it has a
# document to rasterise, and nothing ever cleared them up or hid them.

def _drafts(world):
    r = client.get("/esign/envelopes?include_drafts=true&status=draft",
                   headers=world["headers"])
    assert r.status_code == 200, r.text
    return r.json()["envelopes"]


def test_the_tracking_list_shows_only_what_was_actually_sent(world):
    """The screen says "every contract you have sent for signature". A preview
    has been sent to nobody, so it does not belong on it."""
    preview = _create(world)
    real = _create(world)
    client.post(f"/esign/envelopes/{real['id']}/send",
                headers=world["headers"], json={})

    listed = client.get("/esign/envelopes", headers=world["headers"]).json()["envelopes"]
    ids = [e["id"] for e in listed]
    assert real["id"] in ids
    assert preview["id"] not in ids, "an unsent preview must not look like a contract in flight"
    assert all(e["status"] != "draft" for e in listed)


def test_a_new_preview_replaces_the_last_one(world):
    """Otherwise every visit to the review screen leaves another orphan holding
    a ~26KB PDF, for a contract that does not exist."""
    first = _create(world)
    second = _create(world)
    third = _create(world)

    ids = {e["id"] for e in _drafts(world)}
    assert third["id"] in ids
    assert first["id"] not in ids and second["id"] not in ids, (
        "superseded previews should be gone, not accumulating")
    assert client.get(f"/esign/envelopes/{first['id']}",
                      headers=world["headers"]).status_code == 404


def _sent(world, title: str):
    """One round, raised and sent — sent immediately because an unsent draft is
    discarded by the next preview (see the two tests further down)."""
    env = _create(world, title=title)
    client.post(f"/esign/envelopes/{env['id']}/send",
                headers=world["headers"], json={})
    return env


def test_the_list_comes_back_one_page_at_a_time(world):
    """Assembling a row means reading its recipients, fields and events, so a
    carrier with a long archive must not be made to pay for all of it to look at
    the last five rounds.

    Scoped by a tag in the title because the carrier in this fixture is shared
    with every other test in the file — the point being made is about the shape
    of a page, not about how many rounds happen to exist."""
    import uuid
    tag = f"Paging {uuid.uuid4().hex[:8]}"
    a = _sent(world, f"{tag} A")
    b = _sent(world, f"{tag} B")
    c = _sent(world, f"{tag} C")

    first = client.get(f"/esign/envelopes?q={tag}&limit=2&offset=0",
                       headers=world["headers"]).json()
    assert first["total"] == 3, first["total"]
    assert first["limit"] == 2 and first["offset"] == 0
    assert len(first["envelopes"]) == 2, "a page is a page, not the whole list"
    assert [e["id"] for e in first["envelopes"]] == [c["id"], b["id"]], "newest first"

    second = client.get(f"/esign/envelopes?q={tag}&limit=2&offset=2",
                        headers=world["headers"]).json()
    assert [e["id"] for e in second["envelopes"]] == [a["id"]]
    assert second["total"] == 3, "the total is of everything, not of the page"

    # The two pages together are the whole list, with nothing seen twice and
    # nothing missed — the property OFFSET paging loses on an unstable sort, and
    # why the ordering breaks ties on id.
    seen = [e["id"] for e in first["envelopes"] + second["envelopes"]]
    assert sorted(seen) == sorted([a["id"], b["id"], c["id"]])
    assert len(set(seen)) == 3


def test_the_search_box_asks_the_server_not_the_page(world):
    """It has to match across the whole archive: a browser filtering the page it
    happens to be holding would report an older round as not existing."""
    import uuid
    tag = uuid.uuid4().hex[:8]
    wanted = _sent(world, f"Marine Binder {tag}")
    other = _sent(world, f"Motor Fleet {tag}")

    r = client.get(f"/esign/envelopes?q=marine+binder+{tag}&limit=10",
                   headers=world["headers"]).json()
    ids = [e["id"] for e in r["envelopes"]]
    assert wanted["id"] in ids, "the match is case-insensitive"
    assert other["id"] not in ids, ids
    assert r["total"] == 1, "the total has to count the MATCHES, or the pager lies"


def test_the_history_of_one_contract_can_be_asked_for_on_its_own(world):
    """The record links into this screen filtered to itself.

    Filtered on the SERVER: the list is capped, so a browser-side filter would
    show an older contract an empty history rather than its rounds."""
    mine = _contract_with_wording(
        world, _wording_docx(world["tenant"], world["broker"]), "Mine.docx")
    theirs = _contract_with_wording(
        world, _wording_docx(world["tenant"], world["broker"]), "Theirs.docx")
    # Sent as each is raised: an unsent draft left lying around is discarded by
    # the next preview (see the two tests below this one).
    a = _create(world, contract_id=mine, source="contract")
    client.post(f"/esign/envelopes/{a['id']}/send",
                headers=world["headers"], json={})
    b = _create(world, contract_id=theirs, source="contract")
    client.post(f"/esign/envelopes/{b['id']}/send",
                headers=world["headers"], json={})

    r = client.get(f"/esign/envelopes?contract_id={mine}", headers=world["headers"])
    assert r.status_code == 200, r.text
    ids = [e["id"] for e in r.json()["envelopes"]]
    assert a["id"] in ids
    assert b["id"] not in ids, "another contract's round must not show here"

    # And unfiltered still means everything — the filter is opt-in.
    every = client.get("/esign/envelopes",
                       headers=world["headers"]).json()["envelopes"]
    assert {a["id"], b["id"]} <= {e["id"] for e in every}


def test_a_sent_contract_is_never_discarded_by_a_later_preview(world):
    """THE safety property of the clean-up. Deleting rows near live contracts is
    how a signing round disappears mid-flight, so this is the test that matters
    more than the two above it."""
    live = _create(world)
    client.post(f"/esign/envelopes/{live['id']}/send",
                headers=world["headers"], json={})

    for _ in range(3):
        _create(world)

    still = client.get(f"/esign/envelopes/{live['id']}", headers=world["headers"])
    assert still.status_code == 200, "a contract out with a signer must survive"
    assert still.json()["status"] == "sent"
    tok = _token(still.json(), "insurer")
    assert tok, "and its link must still resolve"
    assert client.get(f"/esign/sign/{tok}").status_code == 200


# ── the seal ────────────────────────────────────────────────────────────────

def _dev_cert(tmp_path):
    """A throwaway self-signed certificate. Real deployments buy one from a CA;
    the cryptography is identical, only the trust chain differs."""
    import datetime as _dt
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Kavachio Seal (test)")])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(days=1))
            .not_valid_after(now + _dt.timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, content_commitment=True,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=False, crl_sign=False,
                encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    p12 = serialization.pkcs12.serialize_key_and_certificates(
        name=b"kavachio", key=key, cert=cert, cas=None,
        encryption_algorithm=serialization.BestAvailableEncryption(b"test"))
    path = tmp_path / "seal.p12"
    path.write_bytes(p12)
    pem = tmp_path / "seal.pem"
    pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(path), str(pem)


def _sign_through(world, env):
    """Both parties sign, in order. Returns the completed envelope."""
    sent = client.post(f"/esign/envelopes/{env['id']}/send",
                       headers=world["headers"], json={}).json()
    for side, who, role in (("insurer", "Dana Alvarez", "Carrier Admin"),
                            ("broker", "Marco Vance", "Broker Admin")):
        full = client.get(f"/esign/envelopes/{env['id']}",
                          headers=world["headers"]).json() if side == "broker" else sent
        tok = _token(full, side)
        h = _unlock(tok)
        v = _open(tok, h)
        r = client.post(f"/esign/sign/{tok}", headers=h, json={
            "agreed": True, "signature_name": who, "fields": _fill(v, who, role)})
        assert r.status_code == 200, r.text
    return client.get(f"/esign/envelopes/{env['id']}", headers=world["headers"]).json()


def test_a_completed_contract_carries_a_seal_that_covers_the_whole_file(world, tmp_path,
                                                                        monkeypatch):
    """What the removed certificate page was failing to do: make an alteration
    detectable. A page of printed history is as editable as the pages before it;
    a signature over the byte range is not."""
    import io
    from pyhanko.keys import load_cert_from_pemder
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation import validate_pdf_signature
    from pyhanko_certvalidator import ValidationContext

    p12, pem = _dev_cert(tmp_path)
    monkeypatch.setenv("ESIGN_SEAL_P12", p12)
    monkeypatch.setenv("ESIGN_SEAL_P12_PASS", "test")

    env = _create(world)
    pages_before = env["page_count"]
    done = _sign_through(world, env)
    assert done["status"] == "completed"
    assert done["page_count"] == pages_before, "no audit page may be appended"

    pdf = client.get(f"/esign/envelopes/{env['id']}/pdf",
                     headers=world["headers"]).content

    # Trusting our own throwaway root is what a real CA's root does in a real
    # reader; it is the only part of this a production certificate changes.
    vc = ValidationContext(trust_roots=[load_cert_from_pemder(pem)],
                           allow_fetching=False, revocation_mode="soft-fail")

    def verdict(buf):
        sig = PdfFileReader(io.BytesIO(buf)).embedded_signatures[0]
        assert sig.field_name == "KavachioSeal"
        return validate_pdf_signature(sig, vc)

    good = verdict(pdf)
    assert good.intact, "the delivered file must verify as untouched"
    assert good.trusted, "and as coming from the certificate we signed with"
    assert good.coverage.name == "ENTIRE_FILE", (
        "a seal covering only part of the file leaves the rest editable")

    tampered = bytearray(pdf)
    tampered[pdf.index(b"stream", 0) + 8] ^= 0xFF
    assert not verdict(bytes(tampered)).intact, (
        "editing one byte MUST break the seal — this is the whole point of it")


def test_a_contract_completes_even_with_no_certificate_configured(world, monkeypatch):
    """A missing certificate must never lose a signature that genuinely
    happened. Sealing is an improvement to the delivered file, not a condition
    of the contract being agreed."""
    monkeypatch.delenv("ESIGN_SEAL_P12", raising=False)
    done = _sign_through(world, _create(world))
    assert done["status"] == "completed"
    assert all(r["status"] == "signed" for r in done["recipients"])


def test_finishing_without_signing_is_refused(world):
    """Pressing Finish without putting a signature anywhere must fail, and say
    which box is empty.

    The browser now stops this with a dialog, but the rule has to hold on the
    server too: the signature box was previously auto-filled from the typed
    name whenever the submission left it out, so a POST carrying no boxes at
    all came back "signed" — the server signing on the signer's behalf.
    """
    env = _create(world)
    sent = client.post(f"/esign/envelopes/{env['id']}/send",
                       headers=world["headers"], json={}).json()
    tok = _token(sent, "insurer")
    h = _unlock(tok)
    view = _open(tok, h)

    # Nothing filled in at all.
    r = client.post(f"/esign/sign/{tok}", headers=h,
                    json={"agreed": True, "signature_name": "Dana Alvarez",
                          "fields": []})
    assert r.status_code == 400, r.text
    assert "Signature" in r.json()["detail"], r.json()

    # Everything EXCEPT the signature — the near miss that matters most.
    everything = _fill(view, "Dana Alvarez", "Carrier Admin")
    sig_ids = {f["id"] for f in view["fields"] if f["mine"] and f["type"] == "signature"}
    r = client.post(f"/esign/sign/{tok}", headers=h, json={
        "agreed": True, "signature_name": "Dana Alvarez",
        "fields": [v for v in everything if v["field_id"] not in sig_ids]})
    assert r.status_code == 400, r.text
    assert "Signature" in r.json()["detail"], r.json()

    # And nothing was recorded by either attempt.
    after = client.get(f"/esign/envelopes/{env['id']}", headers=world["headers"]).json()
    assert after["status"] == "sent"
    assert all(x["status"] != "signed" for x in after["recipients"])


# ═══════════════════════════════════════════════════════════════════════════
#  Signing from inside the app — the flow the negotiation actually feeds
# ═══════════════════════════════════════════════════════════════════════════
#
# The round no longer begins with an email to the carrier. The terms are
# negotiated first (carrier sends → broker agrees), and only then does the
# carrier press Sign and sign it in a new tab, authorised by the seat they are
# already logged into rather than by a code posted to their inbox. The broker
# is emailed once the carrier has actually signed, and can sign in the app too.
#
# These tests are about the two things that flow turns on: that the door is
# opened by WHO you are rather than what you hold, and that it keeps the same
# order as the emailed one.

BROKER_TOKEN_CACHE: dict = {}


def _broker_headers(world):
    """The broker admin's own seat — a different principal, not a scoped
    version of the carrier's."""
    if "h" not in BROKER_TOKEN_CACHE:
        with SessionLocal() as s:
            marco = (s.query(AppUser)
                     .filter(AppUser.broker_party_id == world["broker"],
                             AppUser.role == "broker_admin")
                     .order_by(AppUser.id).first())
            BROKER_TOKEN_CACHE["h"] = {
                "Authorization":
                    f"Bearer {mint_access_token(marco.id, None, 'broker_admin')}"}
    return BROKER_TOKEN_CACHE["h"]


def _authored_contract(world, lifecycle: str = "agreed") -> int:
    """A contract as steps 1–3 leave one: terms and wording held as data, no
    file anywhere, and the two sides having settled it."""
    with SessionLocal() as s:
        c = Contract(
            tenant_id=world["tenant"], program_id=world["program"],
            broker_party_id=world["broker"], status="drafted",
            schedule_key="Schedule A — 2027", lifecycle=lifecycle,
            commercial_terms={"commission_max_pct": "11"},
            wording_sections={"sections": [
                {"title": "Parties and cover",
                 "body": "This Agreement is made between the Carrier and the "
                         "Broker."},
                {"title": "Authority",
                 "body": "The Broker may bind risks within the limits set out "
                         "in the Schedule."}]})
        s.add(c); s.commit()
        return c.id


def _in_app(contract_id: int, headers) -> tuple[int, dict]:
    r = client.post(f"/esign/contracts/{contract_id}/signing-session",
                    headers=headers)
    return r.status_code, (r.json() if r.content else {})


def _sign_with(session: dict, who: str, title: str) -> dict:
    """Sign through the session the in-app door handed out — the same public
    endpoints an emailed link uses, with no code ever typed."""
    tok, h = session["token"], {"X-Esign-Session": session["session"]}
    v = _open(tok, h)
    r = client.post(f"/esign/sign/{tok}", headers=h, json={
        "agreed": True, "signature_name": who, "signature_image": TINY_PNG,
        "fields": _fill(v, who, title)})
    assert r.status_code == 200, r.text
    return r.json()


def test_an_authored_contract_is_composed_with_its_signing_boxes(world):
    """The join between the two halves of the flow. An authored contract has no
    file to send — its wording is sections on a row — so the round composes it,
    and the boxes have to come back out of what it composed."""
    cid = _authored_contract(world)
    code, out = _in_app(cid, world["headers"])
    assert code == 200, out

    env = client.get(f"/esign/envelopes/{out['envelope_id']}",
                     headers=world["headers"]).json()
    keys = {f["party_key"] for f in env["fields"]}
    assert keys == {f"tenant:{world['tenant']}", f"broker:{world['broker']}"}, (
        "both parties must have boxes, read out of the composed document")
    assert any(f["type"] == "signature" for f in env["fields"])


def test_the_carrier_signs_without_a_code_and_without_an_email(world):
    """The point of the whole change: nothing is emailed to the carrier, and
    nothing is typed to get in. The seat is the credential."""
    cid = _authored_contract(world)
    before = len(ISSUED_CODES)
    code, out = _in_app(cid, world["headers"])
    assert code == 200, out
    # A code is minted with the link so the token alone opens nothing — but it
    # is never sent anywhere and never has to be typed.
    assert out["session"], "the carrier is let straight in"

    v = _open(out["token"], {"X-Esign-Session": out["session"]})
    assert v["locked"] is False
    assert len(ISSUED_CODES) - before <= 1, "no second code went anywhere"


def test_the_broker_cannot_open_a_round_the_carrier_has_not_started(world):
    """A round is the carrier's to open. A broker asking first is told so,
    rather than being handed an envelope nobody meant to send."""
    cid = _authored_contract(world)
    code, out = _in_app(cid, _broker_headers(world))
    assert code == 409, out
    assert "not sent this for signature" in out["detail"]


def test_the_broker_cannot_sign_before_the_carrier_has(world):
    """The order the emailed round keeps, kept by the in-app door too — the
    broker signs a document that already carries the carrier's signature."""
    cid = _authored_contract(world)
    assert _in_app(cid, world["headers"])[0] == 200      # carrier opens it
    code, out = _in_app(cid, _broker_headers(world))
    assert code == 409, out
    assert "not your turn" in out["detail"].lower()


def test_signing_moves_the_contract_into_the_other_sides_queue(world):
    """What makes the broker's dashboard light up. The signature is written
    onto the CONTRACT as it happens, not saved up for the end of the round —
    the gap between the two signatures is exactly when the broker needs it."""
    import contract_routes as cr

    cid = _authored_contract(world)
    _, out = _in_app(cid, world["headers"])
    _sign_with(out, "Dana Alvarez", "Carrier Admin")

    with SessionLocal() as s:
        c = s.get(Contract, cid)
        sigs = cr._signatures(s, cid)
        assert [g.side for g in sigs] == ["carrier"]
        assert sigs[0].method == "typed"
        assert cr._whose_turn(c, cr._unsigned_sides(sigs)) == "broker"


def test_the_broker_signs_in_app_and_the_contract_goes_in_force(world):
    """End to end, both sides in the app, no code typed by either. The second
    signature is what puts the contract in force — nothing else does."""
    import contract_routes as cr

    cid = _authored_contract(world)
    _, mine = _in_app(cid, world["headers"])
    _sign_with(mine, "Dana Alvarez", "Carrier Admin")

    code, theirs = _in_app(cid, _broker_headers(world))
    assert code == 200, theirs
    done = _sign_with(theirs, "Marco Vance", "Broker Admin")
    assert done["status"] == "completed"

    with SessionLocal() as s:
        c = s.get(Contract, cid)
        assert sorted(g.side for g in cr._signatures(s, cid)) == [
            "carrier", "counterparty"]
        assert cr._effective_lifecycle(c) == "active"
        assert c.executed_date is not None


def test_terms_still_being_settled_cannot_be_signed(world):
    """Signing a proposal is signing something the other side is still arguing
    with. This is the gate the whole negotiation exists to reach."""
    # Matched on the part of each refusal that IS the reason, not the whole
    # sentence — the draft one gained an "or skip review" escape hatch, and a
    # test that pins the prose fails on a wording change that fixed nothing.
    for state, phrase in (("draft", "terms are not settled"),
                          ("in_review", "still reading the terms"),
                          ("changes_requested", "asked for changes")):
        cid = _authored_contract(world, lifecycle=state)
        code, out = _in_app(cid, world["headers"])
        assert code == 409, (state, out)
        assert phrase in out["detail"], (state, out["detail"])


def test_a_stranger_to_the_contract_is_not_let_in(world):
    """The seat decides which party you are, so a seat on neither side is not a
    signer — and is told the contract does not exist rather than that it does."""
    with SessionLocal() as s:
        other = Tenant(tenant_name=f"esign-out-{os.urandom(3).hex()}",
                       legal_name="Somebody Else Ltd")
        s.add(other); s.commit()
        founder = (s.query(AppUser).filter(AppUser.role == "kavachio_admin")
                   .order_by(AppUser.id).first())
        nosy = AppUser(tenant_id=other.id, email=f"nosy-{os.urandom(3).hex()}@x.test",
                       full_name="Nosy Parker", role="carrier_admin",
                       invited_by_user_id=founder.id)
        s.add(nosy); s.commit()
        h = {"Authorization":
             f"Bearer {mint_access_token(nosy.id, other.id, 'carrier_admin')}"}

    cid = _authored_contract(world)
    code, out = _in_app(cid, h)
    assert code == 404, out


def test_the_typed_signature_path_stands_aside_for_a_live_round(world):
    """Two doors onto one fact is how a contract ends up claiming a signature
    that is not on the document. Once a round is open it owns the signature,
    and the older path says so instead of writing a second row."""
    cid = _authored_contract(world)
    assert _in_app(cid, world["headers"])[0] == 200

    r = client.post(f"/contracts/{cid}/sign", headers=world["headers"],
                    json={"signer_name": "Dana Alvarez"})
    assert r.status_code == 409, r.text
    assert "out for electronic signature" in r.json()["detail"]


def test_the_broker_is_emailed_the_moment_the_carrier_has_signed(world):
    """The handover is the whole reason the carrier goes first. Nothing reaches
    the broker until there is a document carrying the carrier's signature — and
    then it reaches them by email AND on their dashboard."""
    cid = _authored_contract(world)
    _, mine = _in_app(cid, world["headers"])

    env = client.get(f"/esign/envelopes/{mine['envelope_id']}",
                     headers=world["headers"]).json()
    broker = next(r for r in env["recipients"] if r["side"] == "broker")
    assert broker["status"] == "pending" and not broker.get("link"), (
        "the broker must have nothing at all until the carrier has signed")

    _sign_with(mine, "Dana Alvarez", "Carrier Admin")

    env = client.get(f"/esign/envelopes/{mine['envelope_id']}",
                     headers=world["headers"]).json()
    broker = next(r for r in env["recipients"] if r["side"] == "broker")
    assert broker["status"] == "sent" and broker["link"], "their link goes out now"


def test_opening_in_app_does_not_kill_the_link_already_in_the_inbox(world):
    """A broker signing on a laptop must not break the link they were about to
    tap on a phone. The in-app door adds a session to the live token; it does
    not mint a new one, which would retire the emailed one."""
    cid = _authored_contract(world)
    _, mine = _in_app(cid, world["headers"])
    _sign_with(mine, "Dana Alvarez", "Carrier Admin")

    env = client.get(f"/esign/envelopes/{mine['envelope_id']}",
                     headers=world["headers"]).json()
    emailed = _token(env, "broker")

    code, theirs = _in_app(cid, _broker_headers(world))
    assert code == 200, theirs
    assert theirs["token"] == emailed, (
        "the emailed link must still be the live one")


def test_a_different_person_on_the_same_side_signs_as_themselves(world):
    """The boxes are matched on the PARTY, so a colleague may sign for it — but
    the round must then say who actually did. A signature recorded under the
    name of whoever the round happened to guess at is a false statement, and
    the recipient row is what everything downstream reads for the name."""
    with SessionLocal() as s:
        dana = s.get(AppUser, world["carrier_admin"])
        colleague = AppUser(
            tenant_id=world["tenant"], email=f"pri-{os.urandom(3).hex()}@insurisk.test",
            full_name="Priya Shah", role="carrier_admin",
            invited_by_user_id=dana.id)
        s.add(colleague); s.commit()
        h = {"Authorization":
             f"Bearer {mint_access_token(colleague.id, world['tenant'], 'carrier_admin')}"}
        her_email = colleague.email

    cid = _authored_contract(world)
    assert _in_app(cid, world["headers"])[0] == 200      # Dana opens the round
    code, hers = _in_app(cid, h)                         # Priya signs it
    assert code == 200, hers

    env = client.get(f"/esign/envelopes/{hers['envelope_id']}",
                     headers=world["headers"]).json()
    insurer = next(r for r in env["recipients"] if r["side"] == "insurer")
    assert insurer["email"] == her_email
    assert insurer["name"] == "Priya Shah"


# ── how the signature itself was made ───────────────────────────────────────
#
# A signature reaches the document as a data URL, and there are now three ways
# to make one: type it, draw it, or upload a photo of one. The first two are
# produced by a canvas and are always a well-formed PNG. The third is a file the
# signer chose, which means for the first time the picture can be a thing that
# is not a picture — and esign_pdf's response to an image it cannot decode is to
# stamp the typed name instead, silently.
#
# That silence is right for a corrupt drawing (a canvas glitch must not fail a
# signing) and wrong for an upload: the signer picked a file, watched a preview
# of it, and would have no way of learning the document does not carry it. So
# the shape is checked at the door, and these hold the door shut.

# A real 2×2 JPEG. Not a PNG: the point is that the check accepts the formats a
# signer actually has on file, not only the one the drawing canvas emits.
TINY_JPEG = ("data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDACgcHiMe"
             "GSgjISMtKygwPGRBPDc3PHtYXUlkkYCZlo+AjIqgtObDoKrarYqMyP/L2u71////"
             "m8H////6/+b9//j/2wBDASstLTw1PHZBQXb4pYyl+Pj4+Pj4+Pj4+Pj4+Pj4+Pj4"
             "+Pj4+Pj4+Pj4+Pj4+Pj4+Pj4+Pj4+Pj4+Pj4+Pj4+Pj/wAARCAACAAIDASIAAhEB"
             "AxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIE"
             "AwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkK"
             "FhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4"
             "eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT"
             "1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAA"
             "AAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdh"
             "cRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZH"
             "SElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOk"
             "paanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3"
             "+Pn6/9oADAMBAAIRAxEAPwDKooopgf/Z")


def _first_signer_ready(world):
    """A round, sent, with the insurer unlocked and ready to submit."""
    env = _create(world)
    sent = client.post(f"/esign/envelopes/{env['id']}/send",
                       headers=world["headers"], json={}).json()
    tok = _token(sent, "insurer")
    h = _unlock(tok)
    return env, tok, h, _open(tok, h)


def test_an_uploaded_photo_of_a_signature_is_accepted(world):
    """The whole point of the feature: a JPEG off a phone goes on the page.

    Asserted through pdf_version rather than by reading the PDF — the version
    only moves when stamp_fields has actually rewritten the document, so it is
    the cheapest honest evidence that the image was carried rather than dropped
    on the way in."""
    env, tok, h, view = _first_signer_ready(world)
    before = client.get(f"/esign/envelopes/{env['id']}",
                        headers=world["headers"]).json()["pdf_version"]

    r = client.post(f"/esign/sign/{tok}", headers=h, json={
        "agreed": True, "signature_name": "Dana Whitfield",
        "signature_image": TINY_JPEG,
        "fields": _fill(view, "Dana Whitfield", "Head of Underwriting")})
    assert r.status_code == 200, r.text

    after = client.get(f"/esign/envelopes/{env['id']}", headers=world["headers"]).json()
    assert after["pdf_version"] > before
    insurer = next(x for x in after["recipients"] if x["side"] == "insurer")
    assert insurer["status"] == "signed"


@pytest.mark.parametrize("image, because", [
    ("data:text/plain;base64,aGVsbG8=", "not an image at all"),
    ("data:image/svg+xml;base64,PHN2Zy8+", "a format the PDF cannot embed"),
    ("hello", "not a data URL"),
    ("data:image/png,notbase64", "not base64"),
    ("data:image/png;base64,????", "damaged base64"),
    ("data:image/png;base64,", "empty"),
])
def test_a_signature_that_is_not_a_usable_image_is_refused(world, image, because):
    """Refused, and — this is the half that matters — refused BEFORE anything is
    written. A 400 that had already marked the recipient signed would leave a
    contract signed by someone whose signature is not on it."""
    env, tok, h, view = _first_signer_ready(world)

    r = client.post(f"/esign/sign/{tok}", headers=h, json={
        "agreed": True, "signature_name": "Dana Whitfield",
        "signature_image": image,
        "fields": _fill(view, "Dana Whitfield", "Head of Underwriting")})
    assert r.status_code == 400, f"{because}: {r.status_code} {r.text}"

    after = client.get(f"/esign/envelopes/{env['id']}", headers=world["headers"]).json()
    insurer = next(x for x in after["recipients"] if x["side"] == "insurer")
    assert insurer["status"] != "signed", "refused, but signed anyway"
    assert after["status"] == "sent", "a refused signature must not move the round"


def test_a_signature_image_too_big_for_the_document_is_refused(world):
    """4 MB is esign_pdf's own ceiling. Past it the PDF layer drops the image
    and stamps the typed name, which for an upload is a signature the signer
    believes is on the page and is not — so it is stopped here instead."""
    import base64
    env, tok, h, view = _first_signer_ready(world)
    huge = "data:image/png;base64," + base64.b64encode(b"\x00" * 4_100_000).decode()

    r = client.post(f"/esign/sign/{tok}", headers=h, json={
        "agreed": True, "signature_name": "Dana Whitfield",
        "signature_image": huge,
        "fields": _fill(view, "Dana Whitfield", "Head of Underwriting")})
    assert r.status_code == 400
    assert "4 MB" in r.json()["detail"]


def test_no_image_at_all_still_signs(world):
    """Typing it is still a signature. The new check must not have turned an
    absent image into a missing one."""
    env, tok, h, view = _first_signer_ready(world)
    r = client.post(f"/esign/sign/{tok}", headers=h, json={
        "agreed": True, "signature_name": "Dana Whitfield",
        "signature_image": None,
        "fields": _fill(view, "Dana Whitfield", "Head of Underwriting")})
    assert r.status_code == 200, r.text


# ── a signature and initials are two marks ──────────────────────────────────
# Reported from the signing page: a drawn signature was appearing in the
# initials box as well, because one image was stamped into every box of both
# kinds. On paper those are different acts — a signature closes the document,
# initials acknowledge a page or a clause — and initials that ARE the signature
# let anyone holding one page reproduce the mark on the last one.

def _images_in(pdf: bytes) -> int:
    """How many DISTINCT images the document holds.

    Distinct, not placements: a page inherits the resource list, so counting per
    page multiplies by the page count and says nothing. What matters here is
    whether a second, different mark was put in — one image means every marked
    box got the same one."""
    import fitz
    with fitz.open(stream=pdf, filetype="pdf") as d:
        return len({im[0] for i in range(d.page_count)
                    for im in d.get_page_images(i)})


def _mine_by_type(view) -> dict:
    out: dict[str, int] = {}
    for f in view["fields"]:
        if f["mine"]:
            out[f["type"]] = out.get(f["type"], 0) + 1
    return out


def test_initials_are_a_second_mark_not_the_signature_again(world):
    # initials_every_page, because a document with no initials box cannot show
    # what a signature is being kept out of.
    env = _create(world, initials_every_page=True)
    client.post(f"/esign/envelopes/{env['id']}/send",
                headers=world["headers"], json={})
    full = client.get(f"/esign/envelopes/{env['id']}",
                      headers=world["headers"]).json()
    tok = _token(full, "insurer")
    h = _unlock(tok)
    v = _open(tok, h)
    counts = _mine_by_type(v)
    assert counts.get("signature") and counts.get("initial"), counts

    before = _images_in(client.get(f"/esign/sign/{tok}/pdf", headers=h).content)
    r = client.post(f"/esign/sign/{tok}", headers=h, json={
        "agreed": True, "signature_name": "Dana Alvarez",
        "signature_image": TINY_PNG,          # drawn signature, no drawn initials
        "fields": _fill(v, "Dana Alvarez", "Carrier Admin")})
    assert r.status_code == 200, r.text

    signed = client.get(f"/esign/envelopes/{env['id']}/pdf",
                        headers=world["headers"])
    # ONE image in the whole document, with four initials boxes on it: the
    # signature went in the signature box and nowhere else. Before the fix this
    # was the same drawing stamped into all five.
    assert _images_in(signed.content) - before == 1, (
        "the signature image belongs in the signature boxes and nowhere else")
    assert counts["initial"] >= 1

    # And the initials box carries ITS OWN value, which is not the signature.
    done = client.get(f"/esign/envelopes/{env['id']}",
                      headers=world["headers"]).json()
    ini = [f for f in done["fields"]
           if f["type"] == "initial" and f["owner_side"] == "insurer"]
    assert ini and all(f["value"] == "DA" for f in ini), ini
    assert all(f["value"] != "Dana Alvarez" for f in ini)


def test_drawn_initials_go_in_the_initials_boxes(world):
    """The other half: draw them and they are stamped, still only where they
    belong."""
    env = _create(world, initials_every_page=True)
    client.post(f"/esign/envelopes/{env['id']}/send",
                headers=world["headers"], json={})
    full = client.get(f"/esign/envelopes/{env['id']}",
                      headers=world["headers"]).json()
    tok = _token(full, "insurer")
    h = _unlock(tok)
    v = _open(tok, h)
    counts = _mine_by_type(v)

    before = _images_in(client.get(f"/esign/sign/{tok}/pdf", headers=h).content)
    r = client.post(f"/esign/sign/{tok}", headers=h, json={
        "agreed": True, "signature_name": "Dana Alvarez",
        "signature_image": TINY_PNG,
        # A different image on purpose: two copies of one PNG would be stored
        # once, and the count below would prove nothing.
        "initials_image": TINY_JPEG,
        "fields": _fill(v, "Dana Alvarez", "Carrier Admin")})
    assert r.status_code == 200, r.text

    signed = client.get(f"/esign/envelopes/{env['id']}/pdf",
                        headers=world["headers"])
    # More than one now: the initials are a different drawing from the
    # signature, so the document carries both.
    assert _images_in(signed.content) - before >= 2, (
        "a drawn set of initials is a second mark, and has to reach the page")
    assert counts["signature"] and counts["initial"]


def test_a_broken_initials_image_is_refused_like_a_signature(world):
    """It is stamped into a contract the same way, so it is checked the same
    way. An image only validated on one of the two paths is the one somebody
    will use."""
    env = _create(world)
    client.post(f"/esign/envelopes/{env['id']}/send",
                headers=world["headers"], json={})
    full = client.get(f"/esign/envelopes/{env['id']}",
                      headers=world["headers"]).json()
    tok = _token(full, "insurer")
    h = _unlock(tok)
    v = _open(tok, h)
    r = client.post(f"/esign/sign/{tok}", headers=h, json={
        "agreed": True, "signature_name": "Dana Alvarez",
        "initials_image": "data:image/png;base64,not-actually-an-image",
        "fields": _fill(v, "Dana Alvarez", "Carrier Admin")})
    assert r.status_code == 400, r.text
