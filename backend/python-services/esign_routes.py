"""Create-a-Contract, step 4 — Signatures.

Steps 1–3 write the contract. This is what happens to it afterwards: it goes to
the insurer, the insurer signs, and the SAME document — now carrying that
signature — goes on to the broker. Neither of them needs an account.

TWO ROUTERS, AND THE DIFFERENCE MATTERS
---------------------------------------
`router`        /esign/…       carrier_admin, Bearer token, tenant-scoped.
                               Setting a round up, sending it, watching it.
`public_router` /esign/sign/…  NO authentication at all. The emailed token IS
                               the credential, and it stands for exactly one
                               recipient on exactly one envelope.

THE ONE RULE THE PUBLIC SIDE ENFORCES
-------------------------------------
A signer may read the whole document and may write to NOTHING except the boxes
whose `party_key` equals their own. The insurer's key is `tenant:<tenant_id>`,
the broker's is `broker:<broker_party_id>`, and both were stamped into the
document as invisible anchors before it was ever sent. So:

    field.party_key == recipient.party_key      → theirs, editable
    otherwise                                   → visible, locked, and a POST
                                                  naming it is rejected 403

That check lives in `_own_fields()` and every write path goes through it. The
browser is shown which boxes are whose purely so the page can grey the others
out; it is never believed about it.
"""
from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import secrets
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote

from fastapi import (
    APIRouter, Depends, Header, HTTPException, Query, Request, Response,
)
from pydantic import BaseModel, Field
from sqlalchemy import desc, text

import esign_email
import esign_otp
import esign_pdf
import esign_seal
import storage
from auth_deps import (
    Principal, current_principal, db_role_values, require_role,
    resolve_broker_party_id,
)
from db import (
    AppUser, Contract, ContractSignature, EsignEnvelope, EsignEvent, EsignField,
    EsignRecipient, Party, Program, SessionLocal, Tenant,
    party_key_for_broker, party_key_for_tenant,
)
from esign_pdf import Stamp
from esign_sample_contract import ContractTerms, Limit, build_sample_contract

log = logging.getLogger("bdx.esign")

router = APIRouter(prefix="/esign", tags=["signatures"])
public_router = APIRouter(prefix="/esign/sign", tags=["signatures (public)"])

# How long an emailed signing link stays good. Long enough that a signer on
# leave still finds a live link; short enough that a forwarded mailbox is not a
# permanent way in.
LINK_TTL_DAYS = int(os.getenv("ESIGN_LINK_TTL_DAYS", "14"))

# Rendering a page is fast, but not free, and the signing screen asks for every
# page at once. Cap what a caller can ask for.
MAX_RENDER_SCALE = 3.0


# ============================================================================
# helpers
# ============================================================================
def _base_url() -> str:
    return (os.getenv("APP_BASE_URL", "http://localhost:5173") or "").rstrip("/")


def _sign_link(token: str) -> str:
    return f"{_base_url()}/sign?token={token}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Any) -> Optional[datetime]:
    """Stored timestamps come back naive from some columns and aware from
    others (see app_routes._as_utc for why). Comparing the two raises, so every
    comparison in this module goes through here first."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _iso(dt: Any) -> Optional[str]:
    d = _aware(dt)
    return d.isoformat().replace("+00:00", "Z") if d else None


def _human(dt: Any) -> str:
    d = _aware(dt)
    return f"{d:%d %b %Y %H:%M} UTC" if d else ""


def _client_ip(request: Request) -> Optional[str]:
    """The signer's address, preferring the proxy header when we are behind one.
    Recorded beside the signature, so it is worth getting right — but it is
    evidence, never a permission: nothing is authorised on the strength of it."""
    fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return fwd or (request.client.host if request.client else None)


def _event(s, envelope_id: int, type_: str, *, recipient_id: int | None = None,
           actor: str | None = None, request: Request | None = None,
           detail: dict | None = None) -> None:
    """Record what happened. The certificate page is printed from these rows, so
    a step that is not written here is a step that vanishes from the record."""
    s.add(EsignEvent(
        envelope_id=envelope_id, recipient_id=recipient_id, type=type_,
        actor=actor,
        ip=_client_ip(request) if request else None,
        agent=(request.headers.get("user-agent") if request else None),
        detail=detail, at=_now()))


def _pdf_bytes(env: EsignEnvelope, *, original: bool = False) -> bytes:
    """The document as it stands (or the wording as it was written).

    Blob storage is optional in this deployment (STORAGE_BACKEND=db locally), so
    both a ref and inline bytes are supported and resolve the same way every
    other file in the app does."""
    ref = env.source_pdf_ref if original else env.current_pdf_ref
    inline = env.source_pdf if original else env.current_pdf
    data = storage.resolve_bytes(ref, inline)
    if not data and not original:                 # nothing stamped yet
        data = storage.resolve_bytes(env.source_pdf_ref, env.source_pdf)
    if not data:
        raise HTTPException(404, "this contract has no document attached")
    return data


def _store_pdf(env: EsignEnvelope, data: bytes, *, original: bool) -> None:
    """Write a version of the document and point the envelope at it.

    `build_key` mints a fresh uuid per write, so replacing the current PDF —
    which happens on every signature — leaves the previous blob behind. A
    two-signer round would strand two copies of a contract, so the one being
    replaced is deleted. Best-effort by design: a failed cleanup must never
    fail a signature that has already happened."""
    key = f"esign-{env.id or 'new'}-{'source' if original else 'signed'}.pdf"
    previous = env.source_pdf_ref if original else env.current_pdf_ref
    ref, inline = storage.store_or_keep("contracts", env.tenant_id, key, data,
                                        "application/pdf")
    if original:
        env.source_pdf_ref, env.source_pdf = ref, inline
    else:
        env.current_pdf_ref, env.current_pdf = ref, inline
    if previous and previous != ref:
        storage.delete_blob(previous)


def _download_name(title: str, suffix: str = "") -> str:
    """A filename safe to put in a Content-Disposition header.

    HTTP headers are latin-1, and contract titles are not: "Schedule A — 2027"
    carries an em dash and crashes the response before a byte of PDF is sent.
    So the header gets an ASCII fallback AND an RFC 5987 `filename*`, which is
    what every current browser actually reads — the reader still sees the real
    title, and the old fallback still works where it does not.
    """
    stem = unicodedata.normalize("NFKD", title or "contract")
    ascii_stem = re.sub(r"[^A-Za-z0-9._-]+", "_",
                        stem.encode("ascii", "ignore").decode()).strip("_") or "contract"
    if suffix:
        ascii_stem = f"{ascii_stem}_{suffix}"
    pretty = f"{(title or 'contract').strip()}{(' ' + suffix) if suffix else ''}.pdf"
    return (f'filename="{ascii_stem}.pdf"; '
            f"filename*=UTF-8''{quote(pretty, safe='')}")


# The document types on contract_document that ARE the wording, best first.
# 'contract' is the executed/authored wording; 'endorsement' is a mid-term
# change, which is a thing in its own right and signed as its own round.
# 'reference' is never it — a referenced document is quoted BY the wording, not
# the wording itself, and sending one for signature would put the parties' names
# on somebody else's purchasing guidelines.
WORDING_DOC_TYPES = ("contract", "wording", "endorsement")


def _contract_wording(s, contract: Contract) -> tuple[bytes | None, str | None]:
    """The wording to send for signature, and what it is called.

    Where this lives changed under the contract-management work (migration
    14_contract_documents.sql): a contract no longer HAS to be its file. The
    authored wording is attached as a `contract_document` row — which is also
    what lets an endorsement and the wording it amends both be active at once,
    something a single blob column cannot express.

    So the newest ACTIVE wording document wins, and `contract.blob` is the
    fallback for every contract that predates that change. That is exactly the
    fallback migration 14 describes, stated here in the one place step 4 needs
    it. Read with raw SQL because `contract_document` is a canonical table with
    no ORM class, and the columns migration 14 added are not in data_model.py.
    """
    row = s.execute(text("""
        SELECT contract_document_blob_reference,
               contract_document_blob,
               contract_document_filename
          FROM contract_document
         WHERE contract_document_contract_id = :cid
           AND COALESCE(contract_document_is_active, TRUE)
           AND contract_document_type::text = ANY(CAST(:types AS text[]))
      -- CAST(...) rather than `:types::text[]`: SQLAlchemy's text() reads the
      -- second colon of `::` as the start of another bind parameter.
      ORDER BY array_position(CAST(:types AS text[]), contract_document_type::text),
               contract_document_effective_from DESC NULLS LAST,
               contract_document_id DESC
         LIMIT 1
    """), {"cid": contract.id, "types": list(WORDING_DOC_TYPES)}).fetchone()
    if row:
        data = storage.resolve_bytes(row[0], row[1])
        if data:
            return data, (row[2] or contract.filename)
    return (storage.resolve_bytes(contract.blob_ref, contract.blob),
            contract.filename)


def _own_fields(fields: list[EsignField], rec: EsignRecipient) -> list[EsignField]:
    """THE rule. A box is this signer's when its party_key is their party_key —
    'tenant:<tenant_id>' for the insurer, 'broker:<broker_party_id>' for the
    broker. Nothing else grants access to a box: not the email it was sent to,
    not the order, not what the client claims."""
    return [f for f in fields if f.party_key == rec.party_key]


def _field_json(f: EsignField, *, mine: bool, owner: EsignRecipient | None) -> dict:
    return {
        "id": f.id,
        "party_key": f.party_key,
        "type": f.type,
        "page": f.page,
        "x": f.x, "y": f.y, "w": f.w, "h": f.h,
        "required": bool(f.required),
        "label": f.label,
        # Whether the CALLER may fill it. Sent so the page can grey out what is
        # not theirs; the server re-derives it on every write regardless.
        "mine": mine,
        "value": f.value,
        "filled": bool(f.value),
        # Who it belongs to, in words, so a locked box can say whose it is
        # instead of just refusing to be clicked.
        "owner_name": owner.name if owner else None,
        "owner_org": owner.org if owner else None,
        "owner_side": owner.side if owner else None,
    }


def _recipient_json(r: EsignRecipient, *, include_link: bool = False) -> dict:
    out = {
        "id": r.id, "side": r.side, "party_key": r.party_key,
        "tenant_id": r.tenant_id, "broker_party_id": r.broker_party_id,
        "name": r.name, "email": r.email, "title": r.title, "org": r.org,
        "order": r.order_no, "status": r.status,
        "sent_at": _iso(r.sent_at), "viewed_at": _iso(r.viewed_at),
        "signed_at": _iso(r.signed_at),
        "decline_reason": r.decline_reason,
        "signed_ip": r.signed_ip,
    }
    if include_link and r.token:
        # Only ever returned to the carrier who owns the envelope — it is the
        # "copy the link" affordance for a signer whose mail bounced.
        out["link"] = _sign_link(r.token)
        out["token_expires"] = _iso(r.token_expires)
    return out


def _envelope_json(s, env: EsignEnvelope, *, links: bool = False) -> dict:
    recs = sorted(env.recipients, key=lambda r: (r.order_no, r.id or 0))
    by_key = {r.party_key: r for r in recs}
    fields = sorted(env.fields, key=lambda f: (f.page, f.y, f.x))
    nxt = _next_recipient(recs)
    return {
        "id": env.id,
        "title": env.title,
        "status": env.status,
        "contract_id": env.contract_id,
        "program_id": env.program_id,
        "broker_party_id": env.broker_party_id,
        "page_count": env.page_count,
        "pdf_version": env.pdf_version,
        "created_at": _iso(env.created_at),
        "sent_at": _iso(env.sent_at),
        "completed_at": _iso(env.completed_at),
        "waiting_on": (_recipient_json(nxt) if nxt else None),
        "recipients": [_recipient_json(r, include_link=links) for r in recs],
        "fields": [_field_json(f, mine=False, owner=by_key.get(f.party_key))
                   for f in fields],
    }


def _next_recipient(recs: list[EsignRecipient]) -> EsignRecipient | None:
    """Whose turn it is: the lowest-order signer who has not signed. None when
    everybody has, which is what completes the envelope."""
    for r in sorted(recs, key=lambda x: (x.order_no, x.id or 0)):
        if r.status in ("pending", "sent", "viewed"):
            return r
    return None


def _mint_token(r: EsignRecipient) -> str:
    """Issue a fresh link AND the one-time code that unlocks it.

    The two are minted together and expire together, so a live link always has
    a usable code and a dead link cannot be opened with a code kept from an
    older email. Returns the code in PLAIN TEXT — the only moment it exists in
    that form, for the email that is about to go out. What is stored is the
    bcrypt hash, and nothing anywhere logs the plain value.
    """
    r.token = secrets.token_urlsafe(40)
    r.token_expires = _now() + timedelta(days=LINK_TTL_DAYS)
    code = esign_otp.generate_code()
    r.otp_hash = esign_otp.hash_code(code)
    r.otp_expires = r.token_expires
    # A new link is a clean slate: an earlier lockout must not follow the signer
    # onto a link they have only just been given.
    r.otp_attempts = 0
    r.otp_locked_until = None
    r.otp_verified_at = None
    r.otp_sent_count = (r.otp_sent_count or 0) + 1
    r.otp_last_sent_at = _now()
    return code


# ── the unlock gate ─────────────────────────────────────────────────────────
def _session_value(header: str | None, query: str | None) -> str:
    """The unlock session, from wherever this particular request can carry it.

    JSON calls send it as a header. Page images cannot — they are loaded by
    `<img src=…>`, which sets no headers — so those pass it in the query string
    instead. Same value, same check; only the envelope it travels in differs.
    """
    return (header or query or "").strip()


def _require_unlocked(s, token: str, session: str,
                      request: Request | None = None) -> tuple[EsignEnvelope, EsignRecipient]:
    """Resolve the link AND prove the holder typed the code.

    Every route that reveals or changes the contract goes through here. The
    link on its own gets you the lock screen and nothing else.

    Links issued BEFORE the code existed (recipient_otp_hash IS NULL) are let
    through on the link alone. Those were emailed to real signers under the old
    rules, and breaking them would strand people mid-contract with no way
    forward but a phone call; every link minted since carries a code.
    """
    env, rec = _by_token(s, token)
    if not rec.otp_hash:
        return env, rec
    holder = esign_otp.read_session(session, token)
    if holder != rec.id:
        raise HTTPException(
            401, "Enter the code from your email to open this contract.")
    return env, rec


def _term_text(env: EsignEnvelope, s) -> str:
    if not env.contract_id:
        return ""
    c = s.get(Contract, env.contract_id)
    if not c or not (c.inception_dt or c.expiry_dt):
        return ""
    a = f"{c.inception_dt:%d %b %Y}" if c.inception_dt else "?"
    b = f"{c.expiry_dt:%d %b %Y}" if c.expiry_dt else "?"
    return f"{a} to {b}"


def _programme_name(env: EsignEnvelope, s) -> str:
    if not env.program_id:
        return ""
    p = s.get(Program, env.program_id)
    return (getattr(p, "name", None) or "") if p else ""


def _safe_send(to: str, subject: str, html: str, text: str,
               attachments=None) -> bool:
    """Email that can fail without failing the request.

    A signature that is already recorded must not be rolled back because SMTP
    was down — the carrier can resend from the Signatures screen. Failures are
    logged loudly and reported back in the response so nobody assumes a mail
    arrived that did not.

    Sent as the NOTIFY account (NOTIFY_SMTP_USER / _PASS / _FROM), the same
    mailbox platform notices go out from — not the default SMTP_* sender, which
    is a different company's address. A signing request is the first thing a
    broker's signer ever sees from this platform, and the From line is most of
    what tells them it is genuine, so it has to be a Kavachio address. Anything
    the NOTIFY_ prefix does not set falls back to the plain SMTP_* value, so an
    environment with only one mailbox configured still delivers.
    """
    try:
        from email_utils import send_email
        from notifications import NOTIFY_MAIL_ACCOUNT
        send_email(to, subject, html, text=text, attachments=attachments,
                   account=NOTIFY_MAIL_ACCOUNT)
        return True
    except Exception as e:
        log.warning("[esign] email '%s' to %s failed: %s", subject, to, e)
        return False


# ============================================================================
# carrier-side routes  (Bearer token, tenant-scoped)
# ============================================================================
class SignerIn(BaseModel):
    """One signer, as the wizard hands them over.

    `side` decides which identifier is used as the party_key, and the resolution
    happens on the SERVER: a client that sends `party_key` directly is ignored,
    because that string is the whole access rule and a client-chosen one would
    let a broker's link open the insurer's boxes.
    """
    side: str                                  # insurer | broker
    name: str
    email: str
    title: Optional[str] = None
    org: Optional[str] = None
    # Only honoured for a platform admin acting on a tenant, or when the side is
    # broker: an insurer signer is always pinned to the caller's own tenant.
    broker_party_id: Optional[int] = None
    order: Optional[int] = None


class EnvelopeIn(BaseModel):
    title: Optional[str] = None
    contract_id: Optional[int] = None
    program_id: Optional[int] = None
    broker_party_id: Optional[int] = None
    # Signers. Left empty, the two are resolved from the database: the carrier
    # admin making the request, and the broker admin at broker_party_id.
    signers: list[SignerIn] = Field(default_factory=list)
    # Where the document comes from. "sample" generates one that carries the
    # anchors (see esign_sample_contract); "contract" uses the file already
    # stored on the contract row, which is what steps 1–3 will produce.
    source: str = "sample"
    initials_every_page: bool = False
    # Send it as soon as it is built. The Send button on step 3 sets this, which
    # is the whole of the integration with the rest of the wizard.
    send_now: bool = False


class SendIn(BaseModel):
    message: Optional[str] = None


def _resolve_carrier_signer(s, p: Principal, tenant_id: int,
                            given: SignerIn | None) -> SignerIn:
    """Who signs for the insurer. Defaults to the person clicking Send — they
    are a carrier admin of this tenant, which is exactly the authority the
    signature block claims."""
    ten = s.get(Tenant, tenant_id)
    org = (getattr(ten, "legal_name", None) or getattr(ten, "tenant_name", None)
           or "The insurer")
    if given and given.email:
        return SignerIn(side="insurer", name=given.name, email=given.email,
                        title=given.title or "Carrier Admin", org=given.org or org,
                        order=1)
    me = s.get(AppUser, p.user_id)
    if not me or not me.email:
        raise HTTPException(400, "Who is signing for the insurer? Name a signer.")
    return SignerIn(side="insurer", name=me.full_name or me.email, email=me.email,
                    title="Carrier Admin", org=org, order=1)


def _resolve_broker_signer(s, tenant_id: int, broker_party_id: int | None,
                           given: SignerIn | None) -> SignerIn:
    """Who signs for the broker. Defaults to the broker_admin seat attached to
    that broker party — the person the carrier already invited."""
    bpid = (given.broker_party_id if given else None) or broker_party_id
    if not bpid:
        raise HTTPException(400, "Which broker is this contract with?")
    party = s.get(Party, bpid)
    if not party:
        raise HTTPException(404, "that broker is not on file")
    org = party.legal_name or "The broker"
    if given and given.email:
        return SignerIn(side="broker", name=given.name, email=given.email,
                        title=given.title or "Broker Admin", org=given.org or org,
                        broker_party_id=bpid, order=2)
    admin = (s.query(AppUser)
             .filter(AppUser.broker_party_id == bpid,
                     AppUser.role.in_(db_role_values("broker_admin")),
                     AppUser.status != "disabled")
             .order_by(AppUser.id).first())
    if not admin or not admin.email:
        raise HTTPException(
            400, f"{org} has nobody who can sign — invite a broker admin first, "
                 "or name a signer on this request.")
    return SignerIn(side="broker", name=admin.full_name or admin.email,
                    email=admin.email, title="Broker Admin", org=org,
                    broker_party_id=bpid, order=2)


def _discard_stale_drafts(s, tenant_id: int, user_id: int | None) -> int:
    """Throw away this user's earlier unsent previews.

    The review screen has to PERSIST the document it is showing you — the page
    images are rasterised server-side from the stored PDF — so every preview
    writes an envelope row. Nothing ever cleared them up, so previewing a
    contract five times and sending it once left four orphans, each carrying a
    ~26KB PDF, each showing up on the Signatures screen as another "Not sent
    yet" card for a contract that does not exist. That is the duplicate the
    carrier was seeing.

    Scoped hard, because this deletes rows: same carrier, same author, status
    still 'draft', envelope never sent, and not one recipient ever issued a
    link. An envelope that has been out with a signer — at any point, whatever
    its status now — is never touched by this.
    """
    if user_id is None:
        return 0
    stale = (s.query(EsignEnvelope)
             .filter(EsignEnvelope.tenant_id == tenant_id,
                     EsignEnvelope.created_by_user_id == user_id,
                     EsignEnvelope.status == "draft",
                     EsignEnvelope.sent_at.is_(None))
             .all())
    dropped = 0
    for old in stale:
        # Belt and braces over the status check: if anybody was ever emailed a
        # link for this envelope, it is not a preview and it is not ours to
        # delete, whatever the envelope row claims about itself.
        if any(r.sent_at or r.token or r.status != "pending" for r in old.recipients):
            continue
        # Events are not an ORM relationship, so the delete-orphan cascade on
        # recipients and fields does not reach them. The FK has no ON DELETE
        # CASCADE on databases the app created itself, so leaving these behind
        # would fail the delete outright.
        s.query(EsignEvent).filter(EsignEvent.envelope_id == old.id).delete(
            synchronize_session=False)
        s.delete(old)
        dropped += 1
    if dropped:
        log.info("[esign] discarded %s unsent preview(s) for user %s", dropped, user_id)
    return dropped


@router.post("/envelopes")
def create_envelope(body: EnvelopeIn, request: Request,
                    p: Principal = Depends(require_role("carrier_admin"))):
    """Set a signing round up: build the document, work out who signs, and read
    the boxes back out of the document.

    Nothing is emailed unless `send_now` is set — which is what the wizard's
    Send button does, making this one call the whole of step 4's entry point.
    """
    with SessionLocal() as s:
        tenant_id = p.tenant_id
        if tenant_id is None:
            raise HTTPException(403, "no carrier bound to this user")

        contract = None
        if body.contract_id:
            contract = s.get(Contract, body.contract_id)
            if not contract:
                raise HTTPException(404, "contract not found")
            if not p.is_platform_admin and contract.tenant_id != tenant_id:
                raise HTTPException(404, "not found")

        broker_party_id = (body.broker_party_id
                           or (contract.broker_party_id if contract else None))
        program_id = body.program_id or (contract.program_id if contract else None)

        given = {sg.side: sg for sg in body.signers}
        insurer = _resolve_carrier_signer(s, p, tenant_id, given.get("insurer"))
        broker = _resolve_broker_signer(s, tenant_id, broker_party_id,
                                        given.get("broker"))
        broker_party_id = broker.broker_party_id

        # The identities the boxes are matched against. Derived here, from the
        # server's own view of who these organisations are.
        insurer_key = party_key_for_tenant(tenant_id)
        broker_key = party_key_for_broker(broker_party_id)

        # What this round is CALLED. It is the subject line of every email a
        # signer gets and the heading on the page they land on, so it has to be
        # what people call the contract — which for an authored one is its
        # name. That was not consulted at all, so a contract written in
        # Kavachio went out to brokers as "Contract for signature".
        title = (body.title
                 or (contract.name if contract else None)
                 or (contract.schedule_key if contract else None)
                 or (contract.filename if contract else None)
                 or "Contract for signature")

        # A new preview supersedes the last one. Done before the insert so a
        # failure here cannot orphan the envelope we are about to create.
        _discard_stale_drafts(s, tenant_id, p.user_id)

        env = EsignEnvelope(
            tenant_id=tenant_id, contract_id=body.contract_id,
            program_id=program_id, broker_party_id=broker_party_id,
            title=title, status="draft", created_by_user_id=p.user_id,
            created_at=_now(), pdf_version=1,
        )
        s.add(env)
        s.flush()                                # need env.id for the blob key

        pdf = _build_document(s, body, env, insurer, broker,
                              insurer_key, broker_key, contract)
        _store_pdf(env, pdf, original=True)
        _store_pdf(env, pdf, original=False)
        env.page_count = esign_pdf.page_count(pdf)

        recs: dict[str, EsignRecipient] = {}
        for sg, key, order in ((insurer, insurer_key, 1), (broker, broker_key, 2)):
            r = EsignRecipient(
                envelope_id=env.id, side=sg.side, party_key=key,
                tenant_id=tenant_id if sg.side == "insurer" else None,
                broker_party_id=broker_party_id if sg.side == "broker" else None,
                name=sg.name, email=(sg.email or "").strip(), title=sg.title,
                org=sg.org, order_no=sg.order or order, status="pending",
                created_at=_now())
            s.add(r)
            recs[key] = r
        s.flush()

        # Read the boxes out of the document rather than being told where they
        # are. The document is the authority on its own layout.
        placed = esign_pdf.discover_fields(pdf)
        known = set(recs)
        for f in placed:
            if f.party_key not in known:
                # An anchor for somebody who is not on this envelope. Skipping it
                # is deliberate: a box nobody owns can never be filled, and
                # silently assigning it to the nearest signer is how the wrong
                # person ends up signing the wrong block.
                log.warning("[esign] envelope %s: anchor %s names %s, who is not "
                            "a signer here — box ignored",
                            env.id, f.anchor, f.party_key)
                continue
            s.add(EsignField(
                envelope_id=env.id, party_key=f.party_key,
                recipient_id=recs[f.party_key].id, type=f.type, page=f.page,
                x=f.x, y=f.y, w=f.w, h=f.h,
                required=f.type in ("signature", "name", "date"),
                label=f.label, anchor=f.anchor, created_at=_now()))

        _event(s, env.id, "created", actor=insurer.email, request=request,
               detail={"source": body.source, "boxes": len(placed)})
        s.commit()
        envelope_id = env.id
        out = _envelope_json(s, env, links=True)

    # Outside the session: _send_envelope opens its own, and sending mail while
    # another session is still held open is how a slow SMTP server turns into a
    # connection-pool problem.
    if body.send_now:
        return _send_envelope(envelope_id, p, request, note=None)
    return out


def _build_document(s, body: EnvelopeIn, env: EsignEnvelope, insurer: SignerIn,
                    broker: SignerIn, insurer_key: str, broker_key: str,
                    contract: Contract | None) -> bytes:
    """The PDF this round is about.

    Two sources, and the difference is only where the bytes come from — the
    anchors mean the same thing either way:

      "contract"  the contract as steps 1–3 left it. An AUTHORED contract has
                  no file to fetch — its wording is held as sections and
                  composed on demand — so it is composed here, with the signing
                  anchors added, by the same function that composes the copy
                  people download and read. An UPLOADED one is the file on the
                  contract row, used as-is, and it has to carry the anchors
                  itself because nobody here wrote it.
      "sample"    generated here, so step 4 is demonstrable and testable before
                  steps 1–3 exist.
    """
    if body.source == "contract" and contract is not None:
        # Authored in Kavachio: compose it, anchors and all. This is the join
        # between the two halves of the flow — the wording the broker agreed to
        # is the wording that goes out for signature, because there is only one
        # of it and it is built here from the same sections.
        authored = (contract.wording_sections or {}) if isinstance(
            contract.wording_sections, dict) else {}
        if authored.get("sections") or contract.commercial_terms:
            import contract_routes
            return contract_routes.compose_contract_pdf(
                s, contract,
                anchors={"carrier": insurer_key, "counterparty": broker_key})

        data, name = _contract_wording(s, contract)
        if not data:
            raise HTTPException(
                400, "That contract has no wording stored, so there is nothing "
                     "to sign. Attach the wording first, or send the sample.")
        # Steps 1–3 compose the wording as a .docx. Convert once, here, so
        # everything downstream is PDF and step 2 needs no change but its
        # template.
        try:
            data = esign_pdf.ensure_pdf(data, name)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not esign_pdf.discover_fields(data):
            raise HTTPException(
                400, "That wording has no signature blocks in it. Its execution "
                     "page needs a signing anchor for each party — "
                     f"{{{{signature:tenant:{env.tenant_id}}}}} for the insurer and "
                     f"{{{{signature:broker:{env.broker_party_id}}}}} for the broker "
                     "— before it can go out.")
        return data

    ex = (contract.extracted or {}) if contract else {}
    meta = ex.get("program_metadata") if isinstance(ex, dict) else None
    meta = meta if isinstance(meta, dict) else {}

    terms = ContractTerms(
        title=env.title,
        carrier_name=insurer.org or "The insurer",
        carrier_party_key=insurer_key,
        carrier_signer=insurer.name,
        carrier_signer_title=insurer.title or "Carrier Admin",
        broker_name=broker.org or "The broker",
        broker_party_key=broker_key,
        broker_signer=broker.name,
        broker_signer_title=broker.title or "Broker Admin",
        programme=_programme_name(env, s) or meta.get("program_name") or "Programme",
        reference=(contract.schedule_key if contract else None) or "—",
        initials_every_page=body.initials_every_page,
    )
    if contract and contract.inception_dt:
        terms.starts = contract.inception_dt
    if contract and contract.expiry_dt:
        terms.ends = contract.expiry_dt
    if contract and contract.premium_cap_amount:
        cur = contract.premium_cap_currency or terms.currency
        terms.limits.append(Limit(
            "Most premium for the whole term",
            f"{cur} {contract.premium_cap_amount:,.0f}", "warning"))
    return build_sample_contract(terms)


# ============================================================================
# in-app signing  —  the logged-in seat is the credential
# ============================================================================
#
# THE ROUND NO LONGER STARTS WITH AN EMAIL.
#
# The terms are settled first. The carrier sends them out, the broker agrees
# them or pushes back, and only when both sides have stopped moving (lifecycle
# `agreed`) is there anything final to sign. At that point the carrier presses
# Sign on the contract and signs it HERE — a new tab, no email, no one-time
# code — because they are already logged in, and a session this app minted is a
# better answer to "who is this?" than a link that arrived in an inbox. The
# broker is emailed only once the carrier has actually signed, and can sign
# from their own dashboard instead if they would rather.
#
# WHY THE EMAILED LINK KEEPS ITS CODE. Whoever opens that link may have no seat
# here at all, so the link is the only thing tying them to the contract — and a
# link on its own is not enough. In-app, the seat does that job and does it
# better: the party is read off the login rather than off the URL.
#
# HOW THE TWO DOORS MEET. Both end up holding the pair the signing page has
# always used — a link token, and an unlock session bound to it. The in-app
# door hands them straight to a caller it has already identified; the emailed
# door makes them prove the code first. Everything past that point is one code
# path with no idea which door was used: the ownership rule, the turn order,
# the stamping and the seal are unchanged and untouched.


def _my_party_key(s, p: Principal) -> str:
    """Which party this caller signs as — read off the seat, never asked for.

    This is the same string the boxes carry, so taking it from the request
    would hand a broker the insurer's boxes for the asking.

    A broker's party comes from the DATABASE, not the token: mint_access_token
    puts only user/tenant/role in the claims, so Principal.broker_party_id is
    None on every real request. A check written against the attribute would
    compare a real id to None, fail closed, and look exactly like a permission
    decision without being one — see auth_deps.resolve_broker_party_id.
    """
    if p.is_broker:
        bpid = resolve_broker_party_id(s, p)
        if not bpid:
            raise HTTPException(
                403, "your account is not attached to a broker, so there is "
                     "nothing here for you to sign")
        return party_key_for_broker(bpid)
    if p.tenant_id is None:
        raise HTTPException(403, "no carrier bound to this user")
    return party_key_for_tenant(p.tenant_id)


def _contract_for_signing(s, p: Principal, contract_id: int) -> Contract:
    """The contract, if this caller is a party to it.

    Both sides reach the same round, so this cannot lean on the carrier-only
    scoping the rest of the module uses — a broker seat has no tenant_id at
    all. Every refusal is the same 404: which contracts exist is not something
    to leak to somebody who cannot see them.
    """
    missing = HTTPException(404, "contract not found")
    c = s.get(Contract, contract_id)
    if not c:
        raise missing
    if p.is_broker:
        bpid = resolve_broker_party_id(s, p)
        if not bpid or c.broker_party_id != bpid:
            raise missing
    elif not p.is_platform_admin and c.tenant_id != p.tenant_id:
        raise missing
    return c


def _live_round(s, contract_id: int) -> EsignEnvelope | None:
    """The signing round open on this contract, if there is one.

    Drafts are not rounds — those are the previews the review screen persists
    so it has something to rasterise. Voided and declined ones are not either:
    both ended without a signed contract, and the next press of Sign should
    start a fresh round rather than reopen a dead envelope.
    """
    return (s.query(EsignEnvelope)
            .filter(EsignEnvelope.contract_id == contract_id,
                    EsignEnvelope.status.in_(("sent", "in_progress", "completed")))
            .order_by(desc(EsignEnvelope.created_at))
            .first())


# The states a contract can be signed in. `agreed` is the ordinary one — both
# sides settled the terms and the next stop is signature. `signed` is here
# because a round that is half done leaves the contract there, and the second
# signer still has to be able to get in.
SIGNABLE_STATES = ("agreed", "signed")


def _assert_terms_settled(c: Contract) -> None:
    """Refuse to open a round on terms that are still moving.

    Signing a proposal is signing something the other side is still arguing
    with, and it is the failure this whole flow is arranged to prevent: the
    contract goes out for review, comes back agreed, and only then is there one
    document that both sides mean.
    """
    import contract_routes
    state = contract_routes._effective_lifecycle(c)
    if state in SIGNABLE_STATES:
        return
    why = {
        "draft": "the terms are not settled yet. Send them to the broker for "
                 "review — or, if there is nothing to agree, settle them "
                 "yourself with “skip review” and sign straight away.",
        "pending": "it is waiting on the carrier to approve it.",
        "in_review": "the broker is still reading the terms. They agree them "
                     "or ask for changes, and it comes back here to sign.",
        "changes_requested": "the broker has asked for changes. Answer those "
                             "and send the terms out again first.",
        "active": "it is already in force.",
        "expired": "its term has run out.",
        "terminated": "it was ended early.",
        "superseded": "it has been replaced by a renewal.",
    }.get(state, f"it is {state}.")
    raise HTTPException(409, f"This contract cannot be signed yet — {why}")


def _named_signers(c: Contract) -> list[SignerIn]:
    """The people step 4 of the wizard named, as this module spells them.

    The wizard writes them onto the contract's wording as
    {name, email, role, side}; `side` there is carrier/counterparty, which is
    the same two parties this module calls insurer/broker. Anybody named
    without an email is dropped: they can be printed on the signature page but
    they cannot be sent a link, and a recipient row with nowhere to write to is
    a round that silently never arrives.

    Empty is the normal case and not a failure — create_envelope then resolves
    the carrier admin who pressed Sign and the broker admin on the other side,
    which is who would have been named anyway.
    """
    raw = (c.wording_sections or {}) if isinstance(c.wording_sections, dict) else {}
    out: list[SignerIn] = []
    for sg in (raw.get("signers") or []):
        if not isinstance(sg, dict):
            continue
        side = {"carrier": "insurer", "counterparty": "broker"}.get(
            (sg.get("side") or "").strip())
        name = (sg.get("name") or "").strip()
        email = (sg.get("email") or "").strip()
        if not side or not name or not email:
            continue
        # One per side. A second name for the same party is a second signatory
        # on the paper block, not a second link — see contract_wording.
        if any(o.side == side for o in out):
            continue
        out.append(SignerIn(side=side, name=name, email=email,
                            title=(sg.get("role") or "").strip() or None,
                            order=1 if side == "insurer" else 2))
    return out


def _open_round(request: Request, p: Principal, contract_id: int) -> int:
    """Start the signing round for this contract and return its envelope id.

    The document is composed with the anchors, the two recipients are worked
    out, and the envelope is marked as out — but NOTHING IS EMAILED. That is
    the whole change: the carrier signs next, in the tab that is about to open,
    and the broker hears about it when there is a signed document to hear
    about. Sending both links at once is how two people sign two different
    versions of the same contract.
    """
    with SessionLocal() as s:
        c = s.get(Contract, contract_id)
        body = EnvelopeIn(contract_id=c.id, source="contract",
                          broker_party_id=c.broker_party_id,
                          signers=_named_signers(c), send_now=False)

    out = create_envelope(body, request, p)
    envelope_id = int(out["id"])

    with SessionLocal() as s:
        env = s.get(EsignEnvelope, envelope_id)
        # Out of draft, so it shows on the Signatures screen and so a second
        # press of Sign finds it here instead of building another one.
        env.status = "sent"
        env.sent_at = _now()
        _event(s, env.id, "opened_in_app", actor=str(p.user_id), request=request,
               detail={"contract_id": contract_id})
        s.commit()
    return envelope_id


def _admit(s, env: EsignEnvelope, rec: EsignRecipient, request: Request,
           me: AppUser | None) -> tuple[str, str]:
    """Let an identified signer in: the link token, and a session on it.

    THE TOKEN IS REUSED WHEN THERE IS ONE. Minting a fresh link retires the
    old one, and the broker's old one is sitting in their inbox — signing in
    the app on a laptop must not kill the link they were about to tap on a
    phone. So a live token is kept and only a session is added to it.

    THE SESSION IS ISSUED WITHOUT A CODE, and that is not a hole. The code
    exists to prove that whoever holds an emailed URL is the person it was
    sent to. Here that is already established, and better: the caller
    authenticated to this app, and the recipient row was found by matching
    their seat's own party key. Handing them a code to type would be asking
    them to prove something weaker than what they have already proved.

    A token minted here still gets an OTP hash, even though nobody is ever
    told the code. That is deliberate — it means the token ALONE opens
    nothing, so a leaked one is inert without a session, which only an
    authenticated party to this contract can get.
    """
    # WHO IS ACTUALLY SIGNING. The boxes are matched on the PARTY, so anybody
    # holding that party's seat may sign for it — the round was set up naming a
    # likely person (the admin who pressed Sign, or the first broker admin on
    # file), which is a guess, not a rule. When somebody else turns up the guess
    # is corrected rather than left standing: a round that says Marco was asked
    # and Priya signed is two true statements, but one that says Priya's
    # signature IS Marco's is a false one, and it is the recipient row that
    # everything downstream reads for the name.
    #
    # A link already sent to the person being replaced is retired with the
    # change. It named them, it would now open as somebody else, and leaving it
    # live in their inbox is exactly the thing tokens are cleared for.
    if me is not None and (me.email or "").strip().lower() != (
            rec.email or "").strip().lower():
        was = rec.email
        rec.name = me.full_name or me.email
        rec.email = (me.email or "").strip()
        if rec.token:
            _mint_token(rec)
        _event(s, env.id, "signer_changed", recipient_id=rec.id,
               actor=rec.email, request=request,
               detail={"was": was, "party_key": rec.party_key})

    exp = _aware(rec.token_expires)
    if not rec.token or (exp and exp < _now()):
        _mint_token(rec)                  # the code is minted and discarded
    if rec.status == "pending":
        rec.status = "sent"
        rec.sent_at = _now()
    if rec.status in ("sent",):
        rec.viewed_at = rec.viewed_at or _now()
        rec.status = "viewed"
        _event(s, env.id, "viewed", recipient_id=rec.id, actor=rec.email,
               request=request, detail={"via": "in-app"})
    return rec.token, esign_otp.mint_session(rec.id, env.id, rec.token)


class InAppSession(BaseModel):
    """What the signing page needs to open itself without an email."""
    token: str
    session: str
    envelope_id: int
    status: str


@router.post("/contracts/{contract_id}/signing-session",
             response_model=InAppSession)
def signing_session(contract_id: int, request: Request,
                    p: Principal = Depends(current_principal)):
    """Open this contract for signing, as whoever is asking.

    One door for both sides, because the question is the same one: is this
    caller a party to this contract, and is it their turn? The carrier gets a
    round started for them if none is running; the broker never does — a round
    is the carrier's to open, and a broker asking before it has been is told
    so rather than being handed an envelope nobody meant to send.

    What comes back is a token and a session, which the signing page then uses
    exactly as it uses the pair an emailed link and a typed code produce. The
    token is deliberately NOT put in the address bar by the page that receives
    it: it never needs to be, so it never becomes something to forward.
    """
    with SessionLocal() as s:
        c = _contract_for_signing(s, p, contract_id)
        key = _my_party_key(s, p)
        env = _live_round(s, c.id)
        envelope_id = env.id if env is not None else None

        if envelope_id is None:
            if p.is_broker:
                raise HTTPException(
                    409, "the carrier has not sent this for signature yet. You "
                         "will be emailed when it is your turn, and it will "
                         "show on your dashboard.")
            _assert_terms_settled(c)

    # Outside the session on purpose: opening a round composes a PDF and reads
    # boxes back out of it, which is not work to do with a transaction held
    # open. `c` is not touched again out here — it belongs to the session that
    # has just closed.
    if envelope_id is None:
        envelope_id = _open_round(request, p, contract_id)

    with SessionLocal() as s:
        env = s.get(EsignEnvelope, envelope_id)
        if env is None:
            raise HTTPException(404, "that signing round is gone")
        rec = next((r for r in env.recipients if r.party_key == key), None)
        if rec is None:
            # A party to the contract who is not a signer on the round. Says so
            # plainly: silently matching them to the nearest recipient is how
            # the wrong person signs the wrong block.
            raise HTTPException(
                403, "you are not one of the signers named on this contract.")
        _turn_check(env, rec)
        token, session = _admit(s, env, rec, request, s.get(AppUser, p.user_id))
        s.commit()
        return InAppSession(token=token, session=session, envelope_id=env.id,
                            status=env.status)


@router.get("/contracts/{contract_id}/round")
def contract_round(contract_id: int, p: Principal = Depends(current_principal)):
    """Where this contract's signing round has got to, for the button that
    starts or resumes it.

    Answers three things the screen cannot work out for itself: whether a round
    is running, whether it is this caller's turn, and — when it is not — who it
    is waiting on. `can_sign` is the server's answer, so the button and the
    endpoint behind it cannot disagree.
    """
    with SessionLocal() as s:
        c = _contract_for_signing(s, p, contract_id)
        key = _my_party_key(s, p)
        env = _live_round(s, c.id)
        import contract_routes
        state = contract_routes._effective_lifecycle(c)

        if env is None:
            startable = (not p.is_broker) and state in SIGNABLE_STATES
            return {
                "envelope_id": None, "status": None, "started": False,
                "can_sign": startable, "my_turn": startable,
                "i_have_signed": False,
                "waiting_on": ("carrier" if startable else None),
                "waiting_on_name": None,
                "why": (None if startable else
                        ("the carrier has not sent it for signature yet"
                         if p.is_broker else
                         "the terms are not settled yet")),
            }

        rec = next((r for r in env.recipients if r.party_key == key), None)
        turn = _next_recipient(sorted(env.recipients, key=lambda r: r.order_no))
        mine = bool(rec and turn and turn.id == rec.id
                    and env.status not in ("completed", "declined", "voided"))
        return {
            "envelope_id": env.id,
            "status": env.status,
            "started": True,
            "can_sign": mine,
            "my_turn": mine,
            "i_have_signed": bool(rec and rec.status == "signed"),
            "waiting_on": (turn.side if turn else None),
            "waiting_on_name": (turn.org or turn.name) if turn else None,
            "why": (None if mine else
                    ("everybody has signed" if turn is None else
                     f"waiting on {turn.org or turn.name}")),
        }


@router.get("/envelopes")
def list_envelopes(p: Principal = Depends(require_role("carrier_admin")),
                   status: Optional[str] = Query(None),
                   contract_id: Optional[int] = Query(None),
                   q: Optional[str] = Query(None),
                   include_drafts: bool = Query(False),
                   limit: int = Query(50, ge=1, le=200),
                   offset: int = Query(0, ge=0)):
    """Everything this carrier has SENT for signature, and where each one got to.

    Drafts are left out. This screen says it lists contracts that are out for
    signature, and a draft is the opposite of that: it is the preview the review
    screen built so it had something to render, sent to nobody. Showing them
    made every visit to the review page look like another contract in flight.

    `status=draft` or `include_drafts=true` brings them back for anyone who
    genuinely wants to see them.

    `contract_id` narrows it to one contract's rounds, which is how the contract
    record links into this screen. `q` matches the title. Both are filtered HERE
    and not in the browser, because the response is ONE PAGE: a browser-side
    filter would search the page it happens to be holding and report an older
    contract's history as though it did not exist.

    Paged with `limit`/`offset`, returning `total` alongside — assembling an
    envelope means reading its recipients, fields and events, so a carrier with
    a few hundred rounds behind them pays for the whole archive on every visit
    otherwise. Ordering breaks ties on id: two rounds raised in the same second
    with only `created_at` to sort by can swap places between two pages, which
    is how a row shows up twice and another never shows up at all.
    """
    with SessionLocal() as s:
        rows_q = (s.query(EsignEnvelope)
                  .filter(EsignEnvelope.tenant_id == p.tenant_id))
        if contract_id is not None:
            rows_q = rows_q.filter(EsignEnvelope.contract_id == contract_id)
        if status:
            rows_q = rows_q.filter(EsignEnvelope.status == status)
        elif not include_drafts:
            rows_q = rows_q.filter(EsignEnvelope.status != "draft")
        if (q or "").strip():
            rows_q = rows_q.filter(EsignEnvelope.title.ilike(f"%{q.strip()}%"))

        total = rows_q.count()
        rows = (rows_q
                .order_by(desc(EsignEnvelope.created_at), desc(EsignEnvelope.id))
                .limit(limit).offset(offset).all())
        return {"envelopes": [_envelope_json(s, e, links=True) for e in rows],
                "total": total, "limit": limit, "offset": offset}


def _load_envelope(s, envelope_id: int, p: Principal) -> EsignEnvelope:
    env = s.get(EsignEnvelope, envelope_id)
    if not env:
        raise HTTPException(404, "not found")
    if not p.is_platform_admin and env.tenant_id != p.tenant_id:
        raise HTTPException(404, "not found")       # never confirm it exists
    return env


@router.get("/envelopes/{envelope_id}")
def get_envelope(envelope_id: int,
                 p: Principal = Depends(require_role("carrier_admin"))):
    with SessionLocal() as s:
        env = _load_envelope(s, envelope_id, p)
        out = _envelope_json(s, env, links=True)
        out["events"] = [
            {"type": e.type, "at": _iso(e.at), "actor": e.actor, "ip": e.ip,
             "detail": e.detail}
            for e in (s.query(EsignEvent)
                      .filter(EsignEvent.envelope_id == env.id)
                      .order_by(EsignEvent.at, EsignEvent.id).all())]
        return out


@router.get("/envelopes/{envelope_id}/pages/{page_no}")
def envelope_page(envelope_id: int, page_no: int,
                  scale: float = Query(2.0, ge=0.5, le=MAX_RENDER_SCALE),
                  p: Principal = Depends(require_role("carrier_admin"))):
    """One page of the document as it stands, as a PNG."""
    with SessionLocal() as s:
        env = _load_envelope(s, envelope_id, p)
        data = _pdf_bytes(env)
        version = env.pdf_version
    return _png(data, page_no, scale, version)


def _png(pdf: bytes, page_no: int, scale: float, version: int) -> Response:
    try:
        img = esign_pdf.render_page_png(pdf, page_no, scale)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return Response(
        content=img, media_type="image/png",
        headers={
            # Safe to cache hard BECAUSE the url carries the version the caller
            # asked for and every stamp bumps it — a stale image is impossible
            # rather than merely unlikely.
            "Cache-Control": "private, max-age=300",
            "ETag": f'"p{page_no}-v{version}-s{scale}"',
        })


@router.get("/envelopes/{envelope_id}/pdf")
def envelope_pdf(envelope_id: int, original: bool = Query(False),
                 p: Principal = Depends(require_role("carrier_admin"))):
    with SessionLocal() as s:
        env = _load_envelope(s, envelope_id, p)
        data = _pdf_bytes(env, original=original)
        disp = _download_name(env.title, "as written" if original else "signed")
    return Response(content=data, media_type="application/pdf", headers={
        "Content-Disposition": f"attachment; {disp}"})


@router.post("/envelopes/{envelope_id}/send")
def send_envelope(envelope_id: int, body: SendIn, request: Request,
                  p: Principal = Depends(require_role("carrier_admin"))):
    """Send it. Mints a link for the FIRST signer and emails only them.

    Only the first: the broker is emailed when the insurer has actually signed,
    by `_advance()`. Sending both at once is how two people sign two different
    versions of the same contract.
    """
    return _send_envelope(envelope_id, p, request, note=body.message)


def _send_envelope(envelope_id: int, p: Principal, request: Request,
                   note: str | None) -> dict:
    with SessionLocal() as s:
        env = _load_envelope(s, envelope_id, p)
        if env.status in ("completed", "voided"):
            raise HTTPException(409, f"this contract is {env.status} — nothing to send")
        recs = sorted(env.recipients, key=lambda r: (r.order_no, r.id or 0))
        if not recs:
            raise HTTPException(400, "nobody is set to sign this")
        first = _next_recipient(recs)
        if first is None:
            raise HTTPException(409, "everybody has already signed")

        code = _mint_token(first)
        first.status = "sent"
        first.sent_at = _now()
        if env.status == "draft":
            env.status = "sent"
            env.sent_at = _now()

        other = next((r for r in recs if r.id != first.id), None)
        payload = dict(
            link=_sign_link(first.token), signer_name=first.name, title=env.title,
            org_from=(other.org if first.side == "broker" else first.org) or "",
            counterparty=(other.org or other.name) if other else "the other party",
            programme=_programme_name(env, s), term=_term_text(env, s),
            expires_days=LINK_TTL_DAYS, first=(first.order_no <= 1),
            otp=code)
        # The address the token was minted FOR, read off the recipient row — so
        # the link can never be mailed to somebody it does not belong to.
        to, title = first.email, env.title
        _event(s, env.id, "sent", recipient_id=first.id, actor=first.email,
               request=request, detail={"note": note} if note else None)
        s.commit()
        # Built before the session closes: after commit the ORM objects are
        # expired, and reading them outside the block raises.
        env_out = _envelope_json(s, env, links=True)

    ok = _safe_send(to, f"{title} — ready for your signature",
                    esign_email.sign_request_email(**payload),
                    text=f"{title} is ready for your signature: {payload['link']}")
    env_out["emailed"] = ok
    env_out["emailed_to"] = to
    if not ok:
        env_out["email_error"] = (
            "The contract is out for signature but the email did not go. "
            "Copy the link and send it yourself, or try Remind.")
    return env_out


@router.post("/envelopes/{envelope_id}/remind")
def remind(envelope_id: int, request: Request,
           p: Principal = Depends(require_role("carrier_admin"))):
    """Nudge whoever is holding it up, on the link they already have."""
    with SessionLocal() as s:
        env = _load_envelope(s, envelope_id, p)
        rec = _next_recipient(sorted(env.recipients, key=lambda r: r.order_no))
        if rec is None or env.status in ("completed", "voided", "declined"):
            raise HTTPException(409, "there is nobody to remind")
        # A reminder always carries a FRESH link and code. The signer is being
        # written to precisely because they have lost track of the first email,
        # and mailing them a code that is not in the message they are now
        # reading helps nobody. It also retires whatever was in the old mail.
        code = _mint_token(rec)
        if rec.status == "pending":
            rec.status, rec.sent_at = "sent", _now()
        other = next((r for r in env.recipients if r.id != rec.id), None)
        payload = dict(
            link=_sign_link(rec.token), signer_name=rec.name, title=env.title,
            org_from=(other.org if rec.side == "broker" else rec.org) or "",
            counterparty=(other.org or other.name) if other else "the other party",
            programme=_programme_name(env, s), term=_term_text(env, s),
            expires_days=LINK_TTL_DAYS, first=(rec.order_no <= 1), otp=code)
        to, title = rec.email, env.title
        _event(s, env.id, "reminded", recipient_id=rec.id, actor=rec.email,
               request=request)
        s.commit()
    ok = _safe_send(to, f"Reminder — {title} is waiting for your signature",
                    esign_email.sign_request_email(**payload),
                    text=f"{title} is waiting for your signature: {payload['link']}")
    return {"ok": ok, "reminded": to}


@router.post("/envelopes/{envelope_id}/void")
def void_envelope(envelope_id: int, request: Request,
                  p: Principal = Depends(require_role("carrier_admin"))):
    """Pull it back. Every outstanding link stops working immediately."""
    with SessionLocal() as s:
        env = _load_envelope(s, envelope_id, p)
        if env.status == "completed":
            raise HTTPException(409, "this one is already signed by everybody")
        env.status = "voided"
        for r in env.recipients:
            if r.status != "signed":
                # Clearing the token is what actually withdraws it: a link in an
                # inbox somewhere must stop opening the document, not merely be
                # marked as withdrawn in a table nobody consults.
                r.token = None
                r.token_expires = None
        _event(s, env.id, "voided", actor=str(p.user_id), request=request)
        s.commit()
        return _envelope_json(s, env, links=True)


# ============================================================================
# public routes  (the emailed token IS the credential)
# ============================================================================
def _by_token(s, token: str) -> tuple[EsignEnvelope, EsignRecipient]:
    """Resolve a signing link. Every failure returns the same 404 with the same
    wording: a token that exists but has expired must not be distinguishable
    from one that never existed."""
    dead = HTTPException(404, "This signing link is not valid any more. It may "
                              "have expired, been withdrawn, or already been used.")
    if not token or len(token) < 20:
        raise dead
    rec = s.query(EsignRecipient).filter(EsignRecipient.token == token).first()
    if not rec:
        raise dead
    exp = _aware(rec.token_expires)
    if exp and exp < _now():
        raise dead
    env = s.get(EsignEnvelope, rec.envelope_id)
    if not env or env.status in ("voided",):
        raise dead
    return env, rec


def _turn_check(env: EsignEnvelope, rec: EsignRecipient) -> None:
    """A signer may only write when it is actually their turn.

    Without this the broker's link, once issued, would let them sign before the
    insurer had — and the document they signed would not be the one the insurer
    later signed. The order is not a courtesy, it is what keeps one document."""
    if rec.status == "signed":
        raise HTTPException(409, "You have already signed this contract.")
    if rec.status == "declined":
        raise HTTPException(409, "You declined this contract. It is back with the sender.")
    if env.status in ("completed", "declined", "voided"):
        raise HTTPException(409, f"This contract is {env.status}.")
    turn = _next_recipient(sorted(env.recipients, key=lambda r: r.order_no))
    if turn is None or turn.id != rec.id:
        who = turn.org or turn.name if turn else "somebody else"
        raise HTTPException(409, f"It is not your turn yet — waiting on {who}.")


@public_router.get("/{token}")
def open_for_signing(token: str, request: Request,
                     session: str = Query("", alias="session"),
                     x_esign_session: str = Header(default="")):
    """Everything the signing page needs, scoped to the person holding the link.

    The whole document is returned — a signer has to be able to read what they
    are signing — but every box comes back marked `mine` or not, and the ones
    that are not theirs carry the name of the organisation they belong to rather
    than being hidden. Seeing where the other side signs is reassuring; being
    able to sign there is not.
    """
    with SessionLocal() as s:
        env, rec = _by_token(s, token)

        # ── the lock screen ────────────────────────────────────────────────
        # Before the code is entered this returns the bare minimum: that the
        # link is real, which inbox the code went to, and whether the link is
        # currently locked out. No title, no parties, no page count, and above
        # all no document — everything here is visible to whoever holds the URL,
        # and the whole point of the code is that holding the URL is not enough.
        if rec.otp_hash and esign_otp.read_session(
                _session_value(x_esign_session, session), token) != rec.id:
            locked = esign_otp.is_locked(rec.otp_locked_until)
            return {
                "locked": True,
                "email_hint": esign_otp.masked_email(rec.email),
                "attempts_left": (0 if locked else
                                  max(0, esign_otp.MAX_ATTEMPTS - (rec.otp_attempts or 0))),
                "lockout_seconds": esign_otp.lockout_seconds_left(rec.otp_locked_until),
                "can_resend": (rec.otp_sent_count or 0) < esign_otp.MAX_SENDS,
            }

        fields = sorted(env.fields, key=lambda f: (f.page, f.y, f.x))
        by_key = {r.party_key: r for r in env.recipients}
        mine_ids = {f.id for f in _own_fields(fields, rec)}

        if rec.status == "sent":
            rec.status = "viewed"
            rec.viewed_at = _now()
            _event(s, env.id, "viewed", recipient_id=rec.id, actor=rec.email,
                   request=request)
            s.commit()

        turn = _next_recipient(sorted(env.recipients, key=lambda r: r.order_no))
        others = [r for r in env.recipients if r.id != rec.id]
        already = [r for r in env.recipients
                   if r.status == "signed" and r.id != rec.id]
        pdf = _pdf_bytes(env)
        return {
            "locked": False,
            "envelope": {
                "id": env.id, "title": env.title, "status": env.status,
                "page_count": env.page_count or esign_pdf.page_count(pdf),
                "pdf_version": env.pdf_version,
                "pages": esign_pdf.page_sizes(pdf),
                "programme": _programme_name(env, s),
                "term": _term_text(env, s),
            },
            # Who the link thinks you are. The page shows it back so a signer
            # who was forwarded somebody else's link notices immediately.
            "me": {
                "id": rec.id, "name": rec.name, "email": rec.email,
                "title": rec.title, "org": rec.org, "side": rec.side,
                # The identifier the boxes are matched against — shown, not
                # hidden: it is the answer to "why are those my boxes?".
                "party_key": rec.party_key,
                "status": rec.status,
                "my_turn": bool(turn and turn.id == rec.id),
                "signature_name": rec.signature_name or rec.name,
            },
            "others": [
                {"name": r.name, "org": r.org, "side": r.side, "status": r.status,
                 "party_key": r.party_key, "signed_at": _iso(r.signed_at),
                 "order": r.order_no}
                for r in sorted(others, key=lambda r: r.order_no)],
            "already_signed": [
                {"name": r.name, "org": r.org, "signed_at": _human(r.signed_at)}
                for r in already],
            "fields": [
                _field_json(f, mine=(f.id in mine_ids), owner=by_key.get(f.party_key))
                for f in fields],
        }


class VerifyIn(BaseModel):
    code: str


@public_router.post("/{token}/verify")
def verify_code(token: str, body: VerifyIn, request: Request):
    """Type the code from the email; get a short-lived session back.

    A wrong code and an expired code are answered the SAME way, because
    "expired" would confirm to somebody guessing that the link is real. The
    count of remaining attempts IS returned — the person who genuinely mistyped
    needs to know they are running out, and an attacker learns nothing from it
    that five requests would not have told them anyway.
    """
    with SessionLocal() as s:
        env, rec = _by_token(s, token)
        if not rec.otp_hash:
            # Issued before codes existed. Nothing to check, so hand back a
            # session rather than a refusal — the link is still the credential
            # for those, and they are anyway expiring on their own.
            return {"ok": True, "session": esign_otp.mint_session(rec.id, env.id, token),
                    "expires_in": esign_otp.SESSION_MINUTES * 60}

        if esign_otp.is_locked(rec.otp_locked_until):
            raise HTTPException(429, "Too many wrong codes. Try again in "
                                     f"{esign_otp.lockout_seconds_left(rec.otp_locked_until) // 60 + 1}"
                                     " minutes, or ask for a new code.")

        expires = _aware(rec.otp_expires)
        expired = expires is not None and expires < _now()
        if expired or not esign_otp.check_code(body.code or "", rec.otp_hash):
            rec.otp_attempts = (rec.otp_attempts or 0) + 1
            left = esign_otp.MAX_ATTEMPTS - rec.otp_attempts
            if left <= 0:
                rec.otp_locked_until = esign_otp.next_lockout()
                rec.otp_attempts = 0
            _event(s, env.id, "code_failed", recipient_id=rec.id, actor=rec.email,
                   request=request, detail={"locked": left <= 0})
            s.commit()
            if left <= 0:
                raise HTTPException(
                    429, f"Too many wrong codes. This link is locked for "
                         f"{esign_otp.LOCKOUT_MINUTES} minutes.")
            raise HTTPException(
                400, f"That code is not right. {left} attempt"
                     f"{'' if left == 1 else 's'} left.")

        rec.otp_attempts = 0
        rec.otp_locked_until = None
        rec.otp_verified_at = _now()
        _event(s, env.id, "code_verified", recipient_id=rec.id, actor=rec.email,
               request=request)
        session = esign_otp.mint_session(rec.id, env.id, token)
        s.commit()
    return {"ok": True, "session": session,
            "expires_in": esign_otp.SESSION_MINUTES * 60}


@public_router.post("/{token}/resend-code")
def resend_code(token: str, request: Request):
    """Email a fresh code — for the signer who deleted the message or waited
    too long.

    Rate-limited two ways: a cooldown so the button cannot be leaned on, and a
    hard ceiling per link so it can never be turned into a way of mailing
    somebody hundreds of messages. It does NOT re-issue the link: the URL in
    the signer's original email keeps working, so a fresh code is genuinely
    just a fresh code.
    """
    with SessionLocal() as s:
        env, rec = _by_token(s, token)
        last = _aware(rec.otp_last_sent_at)
        if last and (_now() - last).total_seconds() < esign_otp.RESEND_COOLDOWN_SECONDS:
            wait = int(esign_otp.RESEND_COOLDOWN_SECONDS - (_now() - last).total_seconds())
            raise HTTPException(429, f"A code was just sent. Wait {wait} seconds.")
        if (rec.otp_sent_count or 0) >= esign_otp.MAX_SENDS:
            raise HTTPException(
                429, "Too many codes have been sent for this contract. Ask "
                     "whoever sent it to you for a new link.")

        code = esign_otp.generate_code()
        rec.otp_hash = esign_otp.hash_code(code)
        rec.otp_expires = rec.token_expires
        rec.otp_attempts = 0
        rec.otp_locked_until = None
        rec.otp_sent_count = (rec.otp_sent_count or 0) + 1
        rec.otp_last_sent_at = _now()
        to, name, title = rec.email, rec.name, env.title
        _event(s, env.id, "code_sent", recipient_id=rec.id, actor=rec.email,
               request=request)
        s.commit()

    ok = _safe_send(to, f"Your code for {title}",
                    esign_email.code_email(signer_name=name, title=title, otp=code),
                    text=f"Your code for {title} is {code}.")
    return {"ok": ok, "sent_to": esign_otp.masked_email(to)}


@public_router.get("/{token}/pages/{page_no}")
def public_page(token: str, page_no: int,
                scale: float = Query(2.0, ge=0.5, le=MAX_RENDER_SCALE),
                session: str = Query("", alias="session"),
                x_esign_session: str = Header(default="")):
    """One page of the document, for the signing screen. This is the document as
    it STANDS — so the broker's copy already carries the insurer's signature.

    Gated like everything else: a page image IS the contract, and serving one to
    an unlocked link would make the code decorative. The session arrives in the
    query string here because `<img src=…>` cannot set a header."""
    with SessionLocal() as s:
        env, _rec = _require_unlocked(
            s, token, _session_value(x_esign_session, session))
        data = _pdf_bytes(env)
        version = env.pdf_version
    return _png(data, page_no, scale, version)


@public_router.get("/{token}/pdf")
def public_pdf(token: str, session: str = Query("", alias="session"),
               x_esign_session: str = Header(default="")):
    """The whole document as a file. The most valuable thing this API returns,
    so it is behind the code like the rest."""
    with SessionLocal() as s:
        env, _rec = _require_unlocked(
            s, token, _session_value(x_esign_session, session))
        data = _pdf_bytes(env)
        disp = _download_name(env.title)
    return Response(content=data, media_type="application/pdf", headers={
        "Content-Disposition": f"inline; {disp}"})


class FieldValueIn(BaseModel):
    field_id: int
    value: Optional[str] = None


# What a signature image may be. PNG is what the browser produces from both the
# drawing canvas and a cleaned-up upload; the other two are here because a
# signer who has one on file may send it straight through some other client.
_SIG_IMAGE_TYPES = ("png", "jpeg", "jpg", "webp", "gif")
# Matched to esign_pdf's own ceiling on decoded bytes. Beyond it the PDF layer
# drops the image and stamps the typed name instead — silently, which is the
# right behaviour for a corrupt drawing and the wrong one for a signer who
# deliberately chose a picture and would never find out it was not used.
_SIG_IMAGE_MAX_BYTES = 4_000_000


def _check_signature_image(value: str | None) -> None:
    """Refuse a signature image that cannot become one, and say why.

    The alternative is what used to happen: anything at all was stored on the
    recipient row, and esign_pdf quietly fell back to the typed name for
    whatever it could not decode. That is correct for a drawing that arrived
    corrupt — the signing must not fail over a canvas glitch — and wrong for
    somebody who UPLOADED a picture of their signature, who has every reason to
    believe the document carries it and no way to discover it does not.

    So the shape is checked here, at the boundary, where it can still be
    explained. Past this point the old silent fallback stands.
    """
    if not value:
        return
    head, _, b64 = value.partition(",")
    kind = head[len("data:image/"):].split(";")[0].lower() if \
        head.startswith("data:image/") else ""
    if not b64 or "base64" not in head or kind not in _SIG_IMAGE_TYPES:
        raise HTTPException(
            400, "That signature is not an image we can put on the document. "
                 "Draw it, type it, or upload a PNG or JPEG.")
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(
            400, "That signature image arrived damaged. Choose it again.")
    if not raw:
        raise HTTPException(
            400, "That signature image is empty. Choose it again.")
    if len(raw) > _SIG_IMAGE_MAX_BYTES:
        raise HTTPException(
            400, "That signature image is too big to go on the document — "
                 "under 4 MB. A photo of the signature alone, rather than of "
                 "the whole page, is usually well under it.")


class SignIn(BaseModel):
    """What the signer submits.

    `signature_image` is a drawn signature as a data URL; `signature_name` is
    the typed fallback. Field VALUES are sent per box so the same submission can
    carry a signature, a printed name, a title and a date — but which boxes may
    appear here is not the client's decision. See the ownership check below.

    `initials_image` is a SECOND mark and is optional. Initials and a signature
    do different jobs on a contract — the signature closes the document, the
    initials acknowledge a page or a clause — so one image was never allowed to
    stand for both: initialling with the signature itself means anyone holding
    one page can reproduce the mark on the last one. Sent, it goes in the
    initials boxes; not sent, those boxes carry their own text value, which the
    browser fills with the signer's initials.
    """
    signature_name: Optional[str] = None
    signature_image: Optional[str] = None
    initials_image: Optional[str] = None
    fields: list[FieldValueIn] = Field(default_factory=list)
    agreed: bool = False


@public_router.post("/{token}")
def sign(token: str, body: SignIn, request: Request,
         x_esign_session: str = Header(default="")):
    """Sign. This is where the ownership rule is actually enforced.

    In order: is the link live, is it this person's turn, are all the boxes they
    named theirs, are their required boxes filled. Only then is anything
    written — and what is written is burned into the PDF, so the next signer
    opens a document that already carries this signature.
    """
    with SessionLocal() as s:
        env, rec = _require_unlocked(s, token, x_esign_session, request)
        _turn_check(env, rec)
        if not body.agreed:
            raise HTTPException(
                400, "Tick the box to confirm you agree to sign electronically.")
        _check_signature_image(body.signature_image)
        _check_signature_image(body.initials_image)

        fields = list(env.fields)
        mine = {f.id: f for f in _own_fields(fields, rec)}

        # ── THE CHECK ──────────────────────────────────────────────────────
        # Every box named in the submission must be one this signer owns. A
        # broker link naming the insurer's signature box is rejected here, and
        # the message says so plainly rather than pretending the box vanished.
        posted = {v.field_id: (v.value or "") for v in body.fields}
        trespass = [fid for fid in posted if fid not in mine]
        if trespass:
            owners = {f.id: f.party_key for f in fields if f.id in trespass}
            log.warning("[esign] envelope %s: %s (%s) tried to fill %s owned by %s",
                        env.id, rec.email, rec.party_key, trespass, owners)
            raise HTTPException(
                403, "Those boxes belong to the other party — you can only fill "
                     "your own. Reload the page and try again.")

        typed = (body.signature_name or rec.signature_name or rec.name or "").strip()
        signed_on = f"{_now():%d %b %Y}"
        for f in mine.values():
            if f.id in posted:
                f.value = posted[f.id]
            # A signature box left out of the submission means NOT SIGNED. It
            # used to be auto-filled from the typed name, which meant a POST
            # carrying no boxes at all still produced a fully signed contract —
            # the server would sign on the signer's behalf. The browser never
            # does that (adopting a signature fills every signature and initial
            # box before submitting), so nothing legitimate relied on it, and
            # the rule now reads the same on both sides: a signature has to be
            # applied deliberately.
            elif f.type == "name" and not f.value:
                f.value = rec.name
            elif f.type == "title" and not f.value:
                f.value = rec.title or ""
            elif f.type == "date" and not f.value:
                f.value = signed_on              # the platform dates it, not the signer
            if f.value:
                f.filled_at = _now()

        missing = [f for f in mine.values() if f.required and not (f.value or "").strip()]
        if missing:
            names = ", ".join(sorted({(m.label or m.type) for m in missing}))
            raise HTTPException(400, f"Still to fill in: {names}.")

        rec.signature_name = typed
        rec.signature_image = body.signature_image
        rec.status = "signed"
        rec.signed_at = _now()
        rec.signed_ip = _client_ip(request)
        rec.signed_agent = (request.headers.get("user-agent") or "")[:400]
        # The link has done its job. Clearing it means a signed contract cannot
        # be re-opened from an old email, on any device it was forwarded to —
        # and it retires the unlock session with it, because a session names the
        # link it was opened with and that link no longer resolves.
        rec.token = None
        rec.token_expires = None
        rec.otp_hash = None
        rec.otp_expires = None

        caption = (f"Signed by {rec.name} · {_human(rec.signed_at)}"
                   + (f" · {rec.signed_ip}" if rec.signed_ip else ""))
        stamps = [
            Stamp(page=f.page, x=f.x, y=f.y, w=f.w, h=f.h, type=f.type,
                  value=f.value or "",
                  # Each mark in its own kind of box. `initials_image` may be
                  # absent — then the initials box stamps its text value, which
                  # is still a different mark from the signature. What never
                  # happens is the signature image landing in an initials box.
                  image=(body.initials_image if f.type == "initial"
                         else body.signature_image if f.type == "signature"
                         else None),
                  caption=(caption if f.type == "signature" else None))
            for f in sorted(mine.values(), key=lambda f: (f.page, f.y))
            if (f.value or "").strip()]
        stamped = esign_pdf.stamp_fields(_pdf_bytes(env), stamps)
        _store_pdf(env, stamped, original=False)
        env.pdf_version = (env.pdf_version or 1) + 1

        _event(s, env.id, "signed", recipient_id=rec.id, actor=rec.email,
               request=request, detail={"party_key": rec.party_key,
                                        "boxes": len(stamps)})
        # Onto the contract as well as onto the round — this is what moves the
        # contract into the other side's queue. See _record_contract_signature.
        _record_contract_signature(s, env, rec)
        follow_up = _advance(s, env, request)
        s.commit()
        status = env.status
        done = status == "completed"

    # Mail goes out AFTER the commit: a slow SMTP server must not hold a
    # transaction open, and a failed send must not roll back a signature that
    # genuinely happened.
    for job in follow_up:
        _safe_send(**job)
    return {
        "ok": True, "status": status,
        "message": ("Signed. Everybody has now signed — the completed contract "
                    "is on its way to you by email."
                    if done else
                    "Signed. Thank you — it has gone on to the other party, and "
                    "you will get a copy when they have signed too."),
    }


def _record_contract_signature(s, env: EsignEnvelope,
                               rec: EsignRecipient) -> None:
    """Write this signature onto the CONTRACT, not only onto the round.

    Two tables answer two different questions and the platform needs both. The
    envelope says how the SIGNING went — who was asked, who opened it, what was
    stamped where, and it is evidence about one document. `contract_signature`
    says the CONTRACT is signed, and that is what everything else reads: whose
    turn it is, whether it may go in force, what shows on the broker's
    dashboard. A round that wrote only its own tables would leave a contract
    both sides had signed that nothing else in the app knew about.

    Written as each signature happens, not saved up for the end. The gap
    between the carrier signing and the broker signing is exactly the moment
    the broker needs the contract in their queue — and that queue is computed
    from these rows.

    NEVER RAISES. It is called with a signature already stamped into the PDF
    and recorded on the recipient. Losing that because of a bookkeeping row
    would be the worst trade in this file.
    """
    if not env.contract_id:
        return
    try:
        import contract_routes as cr

        side = "carrier" if rec.side == "insurer" else "counterparty"
        name = (rec.signature_name or rec.name or rec.email or "").strip()
        if not name:
            return
        c = s.get(Contract, env.contract_id)
        if c is None:
            return

        # The unique index is (contract, side, lower(name)): signing twice is a
        # slip, not a second signatory, and it must not read as one.
        for sg in cr._signatures(s, c.id):
            if sg.side == side and (sg.signer_name or "").lower() == name.lower():
                return

        # `typed`, not `recorded`. The distinction is about whether the
        # signatory was ever in this system: here they were — they either
        # logged in, or proved a one-time code sent to the address the round
        # was addressed to — and they applied the signature themselves. That is
        # what `typed` claims, and it is all it claims.
        signer = (s.query(AppUser)
                  .filter(AppUser.email == rec.email).first()) if rec.email else None
        s.add(ContractSignature(
            tenant_id=c.tenant_id, contract_id=c.id, side=side,
            signer_name=name, signer_title=rec.title or None,
            signer_email=rec.email or None, method="typed",
            by_user_id=(signer.id if signer else None),
            signed_at=rec.signed_at or _now(),
            note=f"Signed electronically in Kavachio — round {env.id}."))
        s.flush()
    except Exception as e:                                   # noqa: BLE001
        log.exception("[esign] envelope %s: could not record the contract "
                      "signature for %s: %s", env.id, rec.email, e)


def _complete_contract(s, env: EsignEnvelope, request: Request) -> None:
    """Everybody has signed: move the contract itself on.

    The signature rows are already there — each was written as it happened — so
    what is left is only the state change that follows from them, and it is the
    same one contract_routes.sign_contract makes when a second signature comes
    through its own door. The same rules, called rather than restated: a
    contract goes to `signed`, and on to `active` only when nothing else is in
    the way. When something is, it stops at `signed` and stays visibly stuck,
    which is better than half-activating a contract with a hole in it.

    NEVER RAISES, for the same reason as above: `_move` refuses illegal
    transitions with an HTTPException, and one raised here would abort the
    transaction that holds a signature somebody has already given.
    """
    if not env.contract_id:
        return
    try:
        import contract_routes as cr
        from db import ContractDocument

        c = s.get(Contract, env.contract_id)
        if c is None:
            return
        sigs = cr._signatures(s, c.id)
        still = cr._unsigned_sides(sigs)
        if still:
            # The round finished but a signature did not reach the contract.
            # Say so and change nothing: moving the lifecycle would claim
            # something the rows do not support.
            log.warning("[esign] envelope %s completed, but contract %s has no "
                        "signature for %s — lifecycle left alone",
                        env.id, c.id, ", ".join(still))
            return

        c.executed_date = c.executed_date or _now().date()
        if cr._effective_lifecycle(c) in ("draft", "pending", "agreed"):
            cr._move(c, "signed")

        docs = (s.query(ContractDocument)
                .filter(ContractDocument.contract_id == c.id).all())
        missing = cr._missing_references(c, docs)
        if cr._effective_lifecycle(c) == "signed" and not missing:
            cr._move(c, "active")
            _event(s, env.id, "in_force", actor="kavachio", request=request,
                   detail={"contract_id": c.id})
        elif missing:
            _event(s, env.id, "signed_not_in_force", actor="kavachio",
                   request=request,
                   detail={"contract_id": c.id,
                           "missing_documents": missing})
    except Exception as e:                                   # noqa: BLE001
        log.exception("[esign] envelope %s: could not move contract %s on "
                      "after signing: %s", env.id, env.contract_id, e)


def _advance(s, env: EsignEnvelope, request: Request) -> list[dict]:
    """Move the round on now that somebody has signed.

    Either there is a next signer — in which case they get the HANDOVER email,
    and the document they open is the one that was just stamped — or there is
    not, and the envelope completes: the document is sealed and the signed
    copy emailed to everybody.

    Returns the mail to send AFTER the transaction commits. Sending inside it
    would mean a slow SMTP server holding a database transaction open, and a
    failed send rolling back a signature that genuinely happened.
    """
    recs = sorted(env.recipients, key=lambda r: (r.order_no, r.id or 0))
    just_signed = [r for r in recs if r.status == "signed"]
    nxt = _next_recipient(recs)
    jobs: list[dict] = []

    if nxt is not None:
        code = _mint_token(nxt)
        nxt.status = "sent"
        nxt.sent_at = _now()
        env.status = "in_progress"
        last = just_signed[-1] if just_signed else None
        _event(s, env.id, "sent", recipient_id=nxt.id, actor=nxt.email,
               request=request, detail={"handover_from": last.party_key if last else None})
        jobs.append(dict(
            to=nxt.email,
            subject=f"{last.org if last else 'The insurer'} has signed {env.title}",
            html=esign_email.handover_email(
                link=_sign_link(nxt.token), signer_name=nxt.name, title=env.title,
                signed_by_org=(last.org if last else ""),
                signed_by_name=(last.name if last else ""),
                signed_at=_human(last.signed_at if last else None),
                programme=_programme_name(env, s), term=_term_text(env, s),
                expires_days=LINK_TTL_DAYS, otp=code),
            text=(f"{last.org if last else 'The insurer'} has signed {env.title}. "
                  f"It is now with you: {_sign_link(nxt.token)}")))
        return jobs

    # Everybody has signed.
    env.status = "completed"
    env.completed_at = _now()
    _event(s, env.id, "completed", actor="kavachio", request=request)
    s.flush()

    # The delivered file is the contract and nothing else.
    #
    # The audit trail used to be printed as an extra page on the back of it. It
    # lives in contract_esign_event instead, where the History drawer reads it
    # and where it can be exported on its own if a dispute ever needs it.
    # Platform telemetry is evidence ABOUT the agreement, not a term OF it, and
    # appending it made every signed contract a page longer than the document
    # the parties actually read and approved.
    final = _pdf_bytes(env)

    # Seal it: one certification signature across the whole file, so any reader
    # can establish for itself that nothing has been altered since — which is
    # the job the printed page was doing badly, since a page of text is as
    # editable as the pages before it.
    #
    # This must be the LAST thing done to these bytes. Any later write breaks
    # the signature, which is exactly why it is applied here and not on send.
    sealed = esign_seal.seal(final, title=env.title, envelope_id=env.id)
    if sealed is not None:
        final = sealed
        _store_pdf(env, final, original=False)
        env.pdf_version = (env.pdf_version or 1) + 1
        _event(s, env.id, "sealed", actor="kavachio", request=request,
               detail=esign_seal.describe())

    # Now move the CONTRACT on. Nothing else in the app watches the envelope, so
    # a round that ended here would be a signed contract whose checks never
    # start running.
    #
    # This used to stamp an approval flag and the ops status straight onto the
    # row. That was wrong once contract management landed: the real state lives
    # in `lifecycle`, with rules about which move is legal. So the move is made
    # through
    # contract_routes, by the rules that own it.
    _complete_contract(s, env, request)

    signers = [f"{r.name} — {r.org} · {_human(r.signed_at)}" for r in recs]
    html = esign_email.completed_email(
        title=env.title, programme=_programme_name(env, s), term=_term_text(env, s),
        signers=signers, app_link=f"{_base_url()}/contracts/signatures")
    name = (re.sub(r"[^A-Za-z0-9._-]+", "_",
                   unicodedata.normalize("NFKD", env.title)
                   .encode("ascii", "ignore").decode()).strip("_")
            or "contract") + "_signed.pdf"
    for r in recs:
        jobs.append(dict(
            to=r.email, subject=f"{env.title} — fully signed", html=html,
            text=f"{env.title} is fully signed. The signed copy is attached.",
            attachments=[(name, final, "application/pdf")]))
    return jobs


class DeclineIn(BaseModel):
    reason: str


@public_router.post("/{token}/decline")
def decline(token: str, body: DeclineIn, request: Request,
            x_esign_session: str = Header(default="")):
    """Refuse to sign, with a reason.

    Declining is not the failure path — it is the negotiation path. The reason
    goes straight back to whoever sent it so one term can be changed and the
    contract re-sent, rather than two versions existing at once.

    NOT WIRED, ON PURPOSE: migration 15_contract_negotiation.sql added
    `contract_approval.approval_proposed_changes` for structured change requests
    ("premium_cap_amount: 5,000,000 -> 7,500,000"), and a decline at the
    signature stage is exactly that kind of request. Writing a contract_approval
    row here would put it in the same thread the contract screen reads. It is
    left out because the action vocabulary and how a signature-stage decline
    should appear in that thread belong to whoever owns the negotiation screen —
    guessing at it would put rows in their table under a name they did not
    choose. The reason is captured either way and nothing is lost by deciding
    later.
    """
    reason = (body.reason or "").strip()
    if len(reason) < 4:
        raise HTTPException(400, "Say what needs to change — the sender only "
                                 "gets your reason, not a phone call.")
    with SessionLocal() as s:
        # Declining kills the contract for everybody, so it needs the code just
        # as much as signing does.
        env, rec = _require_unlocked(s, token, x_esign_session, request)
        _turn_check(env, rec)
        rec.status = "declined"
        rec.decline_reason = reason[:2000]
        rec.token = None
        rec.token_expires = None
        env.status = "declined"
        _event(s, env.id, "declined", recipient_id=rec.id, actor=rec.email,
               request=request, detail={"reason": reason[:500]})
        # Whoever set the round up needs to know, and so does the other side —
        # they were about to be asked to sign something that is now dead.
        creator = s.get(AppUser, env.created_by_user_id) if env.created_by_user_id else None
        to = [a for a in
              [creator.email if creator else None] +
              [r.email for r in env.recipients if r.id != rec.id]
              if a]
        html = esign_email.declined_email(
            title=env.title, declined_by_name=rec.name,
            declined_by_org=rec.org or "", reason=reason,
            app_link=f"{_base_url()}/contracts/signatures")
        title, who = env.title, rec.name
        s.commit()
    for addr in dict.fromkeys(to):
        _safe_send(addr, f"{title} — a change was asked for", html,
                   text=f"{who} declined to sign {title}: {reason}")
    return {"ok": True, "status": "declined",
            "message": "Thank you — your reason has gone back to the sender."}
