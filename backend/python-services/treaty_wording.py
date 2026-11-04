"""
26.3 — each treaty is configured from its SIGNED wording.

Every treaty has its own wording, and until the signed one is supplied the
treaty cannot be configured. This module answers three questions and does one
job:

  Is there a signed wording?   A document only counts when it was explicitly
                               marked as the signed copy AND it passes
                               validation (it opens, and has pages) — or the
                               treaty was signed in Kavachio, whose sealed PDF
                               is the signed wording. Uploading a file is not
                               enough on its own.
  Where does the treaty stand? Three statuses, kept apart: the treaty's own
                               LIFECYCLE (draft … expired), its DOCUMENTATION
                               (missing / received) and its CONFIGURATION
                               (not started → in progress → pending approval →
                               approved). An expired treaty stays expired
                               however complete the other two are.
  What does the wording say?   Read page by page (treaty_reader), every term
                               coming back with the page it was read from and
                               the words it quotes — so the carrier reviewing
                               it can see where each one came from.

  And it CONFIRMS the reviewed terms: they become the treaty's structure (the
  signed wording is the authority on what was agreed), and what was confirmed,
  by whom, from which document and page, is kept on the treaty.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional

log = logging.getLogger("kavachio.treaty.wording")

# Document kinds a treaty carries (contract_routes.DOC_KINDS). "contract" is
# the wording; marked as the signed copy, it is the SIGNED TREATY WORDING.
VALIDATION_LABEL = {
    "valid": "Validated — opens, text readable",
    "no_text": "Opens, but no readable text (a scan?) — terms are entered by hand",
    "unreadable": "Could not be opened — upload it again as a PDF or Word file",
    "missing_file": "The file is missing",
}
# What still counts as the signed wording: a scan is a signed copy too, it
# just cannot be read for its terms.
ACCEPTED = ("valid", "no_text")

CONFIG_LABEL = {"not_started": "Not started", "in_progress": "In progress",
                "pending_approval": "Pending approval", "approved": "Approved"}


def _cr():
    import contract_routes
    return contract_routes


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# ── the document ────────────────────────────────────────────────────────────
def pdf_of(data: bytes, filename: Optional[str]) -> bytes:
    """The document as a PDF — a Word file is converted the way signing does."""
    import esign_pdf
    return esign_pdf.ensure_pdf(data, filename)


def page_texts(pdf: bytes) -> list[str]:
    import fitz
    with fitz.open(stream=pdf, filetype="pdf") as doc:
        return [p.get_text() or "" for p in doc]


def validate(s, d) -> dict:
    """Check a wording document once and remember the answer on it: does it
    open, how many pages, is there text to read. Cached on the document."""
    from sqlalchemy.orm.attributes import flag_modified
    cached = (d.extracted or {}).get("validation") if isinstance(d.extracted, dict) else None
    if cached and cached.get("fingerprint") == d.fingerprint:
        return cached
    data = _cr()._doc_bytes(d)
    if not data:
        v = {"status": "missing_file", "pages": 0, "chars": 0}
    else:
        try:
            texts = page_texts(pdf_of(data, d.filename))
            chars = sum(len(t.strip()) for t in texts)
            v = {"status": "valid" if chars >= 40 else "no_text",
                 "pages": len(texts), "chars": chars}
        except Exception as e:  # noqa: BLE001 — a bad file is the carrier's to replace
            log.info("wording %s could not be opened: %s", d.id, e)
            v = {"status": "unreadable", "pages": 0, "chars": 0}
    v.update({"label": VALIDATION_LABEL[v["status"]], "checked_at": _now(),
              "fingerprint": d.fingerprint})
    d.extracted = {**(d.extracted if isinstance(d.extracted, dict) else {}),
                   "validation": v}
    flag_modified(d, "extracted")
    return v


def signed_document(s, c):
    """The newest active wording document marked as the signed copy."""
    from db import ContractDocument
    return (s.query(ContractDocument)
            .filter(ContractDocument.contract_id == c.id,
                    ContractDocument.kind == "contract",
                    ContractDocument.is_active.is_(True),
                    ContractDocument.is_executed_copy.is_(True))
            .order_by(ContractDocument.created_at.desc(),
                      ContractDocument.id.desc()).first())


def signed_wording(s, c) -> Optional[dict]:
    """The treaty's signed wording, validated — or None. The sealed PDF of a
    completed signing round counts; so does a document explicitly marked as
    the signed copy that opens. Nothing else does."""
    d = signed_document(s, c)
    if d is not None and (d.blob_ref or d.blob is not None):
        v = validate(s, d)
        if v["status"] in ACCEPTED:
            from db import AppUser
            who = s.get(AppUser, d.uploaded_by_user_id) if d.uploaded_by_user_id else None
            return {"source": "signed_copy", "filename": d.filename,
                    "document_id": d.id, "version": d.version,
                    "uploaded_at": _cr()._iso_utc(d.created_at),
                    "uploaded_by": (who.full_name or who.email) if who else None,
                    "validation": v}
    env = _cr()._signed_envelope(s, c.id)
    if env is not None:
        return {"source": "esigned",
                "filename": f"{(c.name or 'treaty').strip()} (signed).pdf",
                "envelope_id": env.id, "version": None,
                "uploaded_at": _cr()._iso_utc(env.completed_at) if env.completed_at else None,
                "uploaded_by": "Signed in Kavachio",
                "validation": {"status": "valid", "label": "Signed and sealed in Kavachio",
                               "pages": env.page_count or 0}}
    return None


def wording_pdf(s, c) -> Optional[bytes]:
    """The signed wording's bytes, as a PDF."""
    w = signed_wording(s, c)
    if w is None:
        return None
    if w["source"] == "esigned":
        import esign_routes
        from db import EsignEnvelope
        return esign_routes._pdf_bytes(s.get(EsignEnvelope, w["envelope_id"]))
    from db import ContractDocument
    d = s.get(ContractDocument, w["document_id"])
    return pdf_of(_cr()._doc_bytes(d), d.filename)


# ── where the treaty stands ─────────────────────────────────────────────────
def configuration(s, c) -> dict:
    """Not started → in progress → pending approval → approved: the reviewed
    terms and the reinsurance bordereau setup, together."""
    import ceded_routes
    su = ceded_routes.setup_of(s, c)
    ex = c.extracted if isinstance(c.extracted, dict) else {}
    reviewed, read = ex.get("wording_review"), ex.get("wording_reading")
    if su is not None and su.status == "active":
        key = "approved"
    elif su is not None and su.status == "pending_approval":
        key = "pending_approval"
    elif su is not None or reviewed or read:
        key = "in_progress"
    else:
        key = "not_started"
    return {"status": key, "label": CONFIG_LABEL[key],
            "terms_confirmed": bool(reviewed),
            "setup": su.status if su is not None else None}


def statuses(s, c) -> dict:
    """The treaty's three statuses, never merged into one."""
    w = signed_wording(s, c)
    return {
        "lifecycle": _cr()._effective_lifecycle(c),
        "documentation": {"status": "received" if w else "missing",
                          "label": "Received" if w else "Missing"},
        "configuration": configuration(s, c),
        "wording": w,
    }


# ── reading it ──────────────────────────────────────────────────────────────
def read(s, c, *, call=None) -> dict:
    """Read the signed wording, page by page, into the treaty's terms — each
    with the page it came from and the words it quotes. Kept on the treaty as
    the latest reading; nothing is configured until it is confirmed."""
    from sqlalchemy import or_
    from sqlalchemy.orm.attributes import flag_modified
    import treaty_reader
    from db import Party
    w = signed_wording(s, c)
    if w is None:
        raise ValueError("no signed wording")
    pdf = wording_pdf(s, c)
    texts = page_texts(pdf)
    if not any(t.strip() for t in texts):
        raise LookupError("no text")
    body = "\n".join(f"=== Page {i} ===\n{t}" for i, t in enumerate(texts, start=1))
    parties = [(pt.id, pt.legal_name) for pt in s.query(Party).filter(
        Party.party_type == "reinsurer",
        or_(Party.tenant_id == c.tenant_id, Party.scope == "global"))]
    got = treaty_reader.read_treaty(body, parties, call=call, with_sources=True,
                                    pages=len(texts))
    reading = {**got, "document": {k: w.get(k) for k in
                                   ("source", "filename", "document_id", "envelope_id",
                                    "version")},
               "pages": len(texts), "read_at": _now()}
    c.extracted = {**(c.extracted if isinstance(c.extracted, dict) else {}),
                   "wording_reading": reading}
    flag_modified(c, "extracted")
    return reading


def record_review(s, c, *, user_id: Optional[int], who: Optional[str],
                  sources: dict, effective_date: Optional[str],
                  before: Optional[dict], after: dict) -> dict:
    """Keep what was confirmed from the wording, by whom, from which page."""
    from sqlalchemy.orm.attributes import flag_modified
    w = signed_wording(s, c) or {}
    review = {"confirmed_at": _now(), "confirmed_by_user_id": user_id,
              "confirmed_by": who, "effective_date": effective_date,
              "document": {k: w.get(k) for k in ("source", "filename", "document_id",
                                                 "envelope_id", "version")},
              "sources": clean_sources(sources),
              "changed": before != after}
    c.extracted = {**(c.extracted if isinstance(c.extracted, dict) else {}),
                   "wording_review": review}
    flag_modified(c, "extracted")
    return review


def clean_sources(raw: Any, pages: Optional[int] = None) -> dict:
    """{path: {page, quote}} — kept to sound page numbers and short quotes."""
    out: dict[str, dict] = {}
    if not isinstance(raw, dict):
        return out
    for path, v in raw.items():
        if not isinstance(v, dict):
            continue
        try:
            page = int(v.get("page"))
        except (TypeError, ValueError):
            continue
        if page < 1 or (pages and page > pages):
            continue
        out[str(path)[:120]] = {"page": page,
                                "quote": (str(v.get("quote") or "").strip()[:300] or None)}
    return out
