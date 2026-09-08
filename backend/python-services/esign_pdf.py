"""The PDF half of Create-a-Contract step 4 — Signatures.

Three jobs, and nothing else:

  1. FIND THE BOXES.  The document carries invisible tokens at the exact spots
     a signature belongs — `{{signature:tenant:12}}`, `{{date:broker:47}}`.
     `discover_fields()` searches for them, turns each hit into a rectangle, and
     reads the OWNER straight out of the token. This is anchor tagging, the same
     trick DocuSign uses, and it is what answers "is this the broker's box or the
     carrier's?" without anybody dragging anything onto a page.

  2. SHOW THE PAGE.  `render_page_png()` rasterises one page so the signing
     screen can lay HTML boxes over a picture of the document. The browser needs
     no PDF library at all, which is why there isn't one in package.json.

  3. WRITE ON IT.  `stamp_fields()` burns a signer's values into the PDF at the
     stored rectangles. The result becomes the document the NEXT signer opens —
     which is how the broker sees the insurer's signature already on the page.

Coordinates crossing the boundary to the database and the browser are always
FRACTIONS of the page (0..1, origin top-left), never points. The signing screen
renders each page at whatever width it is given; fractions land the box in the
same place at every zoom and every DPI, and a page swapped for a differently
sized one keeps its layout proportionally rather than throwing boxes off the
paper.
"""
from __future__ import annotations

import base64
import binascii
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

import fitz  # PyMuPDF

log = logging.getLogger("bdx.esign.pdf")

# --- the anchor vocabulary --------------------------------------------------
# {{<type>:<party kind>:<id>}} — the party half is the party_key stored on both
# the field and the recipient, so the token literally spells out who owns the
# box it marks.
ANCHOR_RE = re.compile(r"\{\{(signature|initial|name|title|date|text):(tenant|broker):(\d+)\}\}")

FIELD_TYPES = ("signature", "initial", "name", "title", "date", "text")

# How big a box of each kind is, in points, measured from the anchor's top-left.
# A signature needs room for a drawn scrawl; a date does not.
BOX_SIZE: dict[str, tuple[float, float]] = {
    "signature": (185.0, 42.0),
    "initial":   (58.0, 32.0),
    "name":      (185.0, 15.0),
    "title":     (185.0, 15.0),
    "date":      (125.0, 15.0),
    "text":      (185.0, 15.0),
}

# What the signer is asked for, in words rather than field names.
FIELD_LABEL: dict[str, str] = {
    "signature": "Signature",
    "initial":   "Initials",
    "name":      "Full name",
    "title":     "Job title",
    "date":      "Date signed",
    "text":      "Text",
}


def anchor_token(field_type: str, party_key: str) -> str:
    """The token to place in a document so `discover_fields` puts a box there."""
    return "{{" + f"{field_type}:{party_key}" + "}}"


@dataclass(frozen=True)
class DiscoveredField:
    """One box the document asked for, in page fractions."""
    party_key: str
    type: str
    page: int          # 1-based, as everybody outside this module counts pages
    x: float
    y: float
    w: float
    h: float
    anchor: str
    label: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "party_key": self.party_key, "type": self.type, "page": self.page,
            "x": self.x, "y": self.y, "w": self.w, "h": self.h,
            "anchor": self.anchor, "label": self.label,
        }


def _open(pdf_bytes: bytes) -> fitz.Document:
    return fitz.open(stream=pdf_bytes, filetype="pdf")


# Word formats the wording can arrive as. Steps 1–3 compose the contract as a
# .docx (see migration 16_contract_authoring.sql), and a .docx cannot be shown
# in a browser, have boxes laid over it, or be stamped — so it becomes a PDF
# here, once, on the way into the envelope. MuPDF's office handler does the
# conversion in-process; no LibreOffice, no subprocess, no new dependency.
OFFICE_FILETYPES: dict[str, str] = {
    ".docx": "docx", ".doc": "doc", ".odt": "odt", ".rtf": "office",
}


def is_pdf(data: bytes) -> bool:
    """A PDF says so in its first five bytes. Sniffed rather than trusted from
    the filename, because the filename is whatever somebody typed."""
    return bool(data) and data[:5] == b"%PDF-"


def ensure_pdf(data: bytes, filename: str | None = None) -> bytes:
    """The document as a PDF, converting a Word/ODF wording if that is what
    arrived.

    Everything downstream — anchor discovery, page images, stamping — is PDF
    work, so this is the single place a format decision is made. A PDF passes
    straight through untouched; a .docx is converted and its text layer (and
    therefore its anchors) survives, which is what lets the wording step 2
    composes be signed without step 2 changing anything but its template.

    Raises ValueError, with a message meant for the person who uploaded the
    file, when the format is one nothing here can read.
    """
    if not data:
        raise ValueError("There is no document to sign.")
    if is_pdf(data):
        return data
    ext = ""
    if filename and "." in filename:
        ext = "." + filename.rsplit(".", 1)[1].lower()
    candidates = [OFFICE_FILETYPES[ext]] if ext in OFFICE_FILETYPES else []
    # No usable extension: a wording is overwhelmingly a Word file, so try the
    # office handler before giving up rather than refusing on a missing suffix.
    candidates.append("office")
    for ft in dict.fromkeys(candidates):
        try:
            with fitz.open(stream=data, filetype=ft) as doc:
                converted = doc.convert_to_pdf()
            if is_pdf(converted):
                log.info("[esign] converted %s (%s) to PDF for signing",
                         filename or "the wording", ft)
                return converted
        except Exception as e:                     # not this format — try the next
            log.debug("[esign] %s is not %s: %s", filename, ft, e)
    raise ValueError(
        f"{filename or 'That document'} is not a format that can be signed. "
        "The wording has to be a PDF or a Word document.")


def page_count(pdf_bytes: bytes) -> int:
    with _open(pdf_bytes) as doc:
        return doc.page_count


def page_sizes(pdf_bytes: bytes) -> list[dict[str, float]]:
    """Width/height of every page in points — the signing screen uses the ratio
    to reserve the right space before the image has loaded, so the boxes don't
    jump once it does."""
    with _open(pdf_bytes) as doc:
        return [{"width": p.rect.width, "height": p.rect.height} for p in doc]


def discover_fields(pdf_bytes: bytes) -> list[DiscoveredField]:
    """Every anchored box in the document, in reading order.

    Anchors are written INVISIBLY (render_mode 3) by the document generator, so
    they are searchable but never appear on the page or in a printout. Nothing
    is redacted here: the token has to survive so a re-generated document can be
    re-tagged and produce the identical layout instead of being re-drawn by hand.
    """
    found: list[DiscoveredField] = []
    with _open(pdf_bytes) as doc:
        for pno, page in enumerate(doc, start=1):
            pw, ph = page.rect.width, page.rect.height
            if pw <= 0 or ph <= 0:
                continue
            text = page.get_text("text")
            # Search only for the tokens this page actually contains: search_for
            # walks the whole page per call, and a contract has few anchors but
            # many pages.
            for m in dict.fromkeys(ANCHOR_RE.findall(text)):
                ftype, kind, ident = m
                token = "{{" + f"{ftype}:{kind}:{ident}" + "}}"
                party_key = f"{kind}:{ident}"
                bw, bh = BOX_SIZE.get(ftype, BOX_SIZE["text"])
                for rect in page.search_for(token):
                    found.append(DiscoveredField(
                        party_key=party_key,
                        type=ftype,
                        page=pno,
                        x=round(rect.x0 / pw, 6),
                        y=round(rect.y0 / ph, 6),
                        w=round(min(bw, pw - rect.x0) / pw, 6),
                        h=round(min(bh, ph - rect.y0) / ph, 6),
                        anchor=token,
                        label=FIELD_LABEL.get(ftype, "Field"),
                    ))
    found.sort(key=lambda f: (f.page, f.y, f.x))
    return found


def render_page_png(pdf_bytes: bytes, page_no: int, scale: float = 2.0) -> bytes:
    """One page as a PNG. `page_no` is 1-based.

    scale 2.0 ≈ 144 DPI: sharp on a retina screen at a readable width, and small
    enough that a four-page contract is a handful of requests rather than a
    download. Clamped, because the scale arrives from a query string.
    """
    scale = max(0.5, min(float(scale or 2.0), 4.0))
    with _open(pdf_bytes) as doc:
        if not (1 <= page_no <= doc.page_count):
            raise ValueError(f"page {page_no} is outside this document (1–{doc.page_count})")
        pix = doc[page_no - 1].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        return pix.tobytes("png")


def _decode_data_url(value: str | None) -> bytes | None:
    """The PNG bytes behind a `data:image/png;base64,…` URL, or None.

    Returns None rather than raising on anything malformed: a signature that
    cannot be decoded must fall back to the typed name, not fail the signing.
    """
    if not value or not isinstance(value, str):
        return None
    head, _, b64 = value.partition(",")
    if not b64 or "base64" not in head:
        return None
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return None
    # A drawn signature is a few KB. Anything enormous is a mistake or an
    # attempt to bloat the document, and neither belongs in the PDF.
    return raw if 0 < len(raw) <= 4_000_000 else None


@dataclass
class Stamp:
    """One value to burn into the page, positioned in page fractions."""
    page: int
    x: float
    y: float
    w: float
    h: float
    type: str
    value: str = ""
    image: str | None = None       # data URL, for a drawn signature
    caption: str | None = None     # the grey audit line under a signature


def stamp_fields(pdf_bytes: bytes, stamps: Sequence[Stamp]) -> bytes:
    """Return the document with every stamp applied.

    The input is never modified — each signature produces a new version of the
    document, and the original wording is kept untouched elsewhere so "what did
    they actually agree to?" stays answerable.
    """
    if not stamps:
        return pdf_bytes
    doc = _open(pdf_bytes)
    try:
        for st in stamps:
            if not (1 <= st.page <= doc.page_count):
                log.warning("[esign] stamp skipped — page %s outside document", st.page)
                continue
            page = doc[st.page - 1]
            pw, ph = page.rect.width, page.rect.height
            rect = fitz.Rect(st.x * pw, st.y * ph,
                             (st.x + st.w) * pw, (st.y + st.h) * ph)
            if st.type in ("signature", "initial"):
                _stamp_signature(page, rect, st)
            else:
                _stamp_text(page, rect, st.value)
        out = doc.tobytes(deflate=True, garbage=3)
    finally:
        doc.close()
    return out


def _stamp_signature(page: fitz.Page, rect: fitz.Rect, st: Stamp) -> None:
    """A drawn signature if there is one, otherwise the typed name in italic.

    Either way a small grey caption goes underneath — who signed and when. That
    line is the difference between a picture of a name and a record of an act,
    and it is what a reader looks for months later.
    """
    img = _decode_data_url(st.image)
    drawn = False
    if img:
        try:
            page.insert_image(_fit(rect, img), stream=img, keep_proportion=True,
                              overlay=True)
            drawn = True
        except Exception as e:                     # a corrupt PNG must not stop a signing
            log.warning("[esign] drawn signature could not be placed (%s) — "
                        "falling back to the typed name", e)
    if not drawn:
        text = (st.value or "").strip()
        if text:
            # Times-Italic ("tiit") reads as a signature where Helvetica reads
            # as a form field. Shrink to fit rather than overflow the block.
            size = 20.0
            while size > 8.0 and fitz.get_text_length(text, "tiit", size) > rect.width - 6:
                size -= 1.0
            page.insert_textbox(rect, text, fontname="tiit", fontsize=size,
                                color=(0.06, 0.09, 0.20), align=fitz.TEXT_ALIGN_LEFT)
    if st.caption:
        # Just above the ruled line, not across it: the caption is a note about
        # the signature, and a line through it reads as part of the signature.
        cap = fitz.Rect(rect.x0, rect.y1 - 10, rect.x0 + max(rect.width, 190), rect.y1 - 1)
        page.insert_textbox(cap, st.caption, fontname="helv", fontsize=5.6,
                            color=(0.45, 0.48, 0.55))


def _fit(box: fitz.Rect, image: bytes) -> fitz.Rect:
    """Where a drawn signature actually goes inside its box.

    The fit is computed here rather than left to `keep_proportion`, for two
    reasons. It stretches a squarish scrawl to fill a wide box — measurably, on
    this version — which turns a signature into a smear. And even when it does
    scale correctly it centres the result, whereas a signature belongs sitting
    on the ruled line at the left, exactly where a pen would have left it.

    An unreadable image falls back to the whole box; `insert_image` will then
    either place it or raise, and the caller already handles raising.
    """
    try:
        pix = fitz.Pixmap(image)
        iw, ih = float(pix.width), float(pix.height)
    except Exception:
        return box
    if iw <= 0 or ih <= 0:
        return box
    scale = min(box.width / iw, box.height / ih)
    w, h = iw * scale, ih * scale
    # Left-aligned, bottom-aligned: the baseline of the box is the ruled line.
    return fitz.Rect(box.x0, box.y1 - h, box.x0 + w, box.y1)


def _stamp_text(page: fitz.Page, rect: fitz.Rect, value: str) -> None:
    text = (value or "").strip()
    if not text:
        return
    size = 9.0
    while size > 5.5 and fitz.get_text_length(text, "helv", size) > rect.width - 2:
        size -= 0.5
    # Nudged down a point: the anchor marks the top of the box, and text sitting
    # flush against a ruled line above it reads as part of the line.
    box = fitz.Rect(rect.x0, rect.y0 + 1, rect.x1, rect.y1 + 4)
    page.insert_textbox(box, text, fontname="helv", fontsize=size,
                        color=(0.06, 0.09, 0.20))


# --- the certificate page ---------------------------------------------------

_MARGIN = 56.0


def append_certificate(pdf_bytes: bytes, *, title: str, envelope_id: int,
                       recipients: Sequence[dict[str, Any]],
                       events: Sequence[dict[str, Any]]) -> bytes:
    """Render the audit page for a completed document.

    NOT WIRED INTO THE SIGNING FLOW. It used to be appended to every completed
    contract; it is not any more, because the delivered file should be the
    agreement and nothing else — an extra page of platform telemetry made every
    signed contract a page longer than the one the parties read and approved.
    Tamper-evidence, which was the other thing it was doing, is now the job of
    the seal in esign_seal.py, and it does it properly: a printed page of
    history is exactly as editable as the pages in front of it.

    Kept because the need it served is real — a contract forwarded to a lawyer,
    an auditor or a reinsurer should be able to arrive with its record, without
    a login to this platform. When that is wanted it should be a SEPARATE
    document offered next to the signed copy, not stapled to the back of it.
    Call it with the rows from contract_esign_event to produce one.
    """
    doc = _open(pdf_bytes)
    try:
        page = doc.new_page(width=595, height=842)
        y = _MARGIN
        y = _cert_line(page, y, "Certificate of completion", size=16, bold=True)
        y = _cert_line(page, y + 2, title, size=10.5, color=(0.34, 0.38, 0.45))
        y = _cert_line(page, y + 1,
                       f"Kavachio envelope #{envelope_id} · issued "
                       f"{datetime.now(timezone.utc):%d %b %Y %H:%M} UTC",
                       size=8.5, color=(0.55, 0.58, 0.64))
        y += 14
        page.draw_line(fitz.Point(_MARGIN, y), fitz.Point(595 - _MARGIN, y),
                       color=(0.85, 0.87, 0.90), width=0.7)
        y += 20

        y = _cert_line(page, y, "Signers", size=11, bold=True)
        y += 6
        for r in recipients:
            who = f"{r.get('name','')} — {r.get('org') or ''}".strip(" —")
            y = _cert_line(page, y, who, size=9.5, bold=True)
            y = _cert_line(page, y, r.get("email", ""), size=8.5,
                           color=(0.34, 0.38, 0.45))
            # The identifier the boxes were matched against. Printing it is the
            # point: it is the reason each signature is on the right block.
            y = _cert_line(page, y, f"Signing as {r.get('party_key','')}"
                           f"  ·  {r.get('status','')}"
                           + (f"  ·  {r['signed_at']}" if r.get("signed_at") else ""),
                           size=8, color=(0.55, 0.58, 0.64))
            if r.get("signed_ip"):
                y = _cert_line(page, y, f"From {r['signed_ip']}", size=8,
                               color=(0.55, 0.58, 0.64))
            y += 10
            if y > 700:
                page = doc.new_page(width=595, height=842); y = _MARGIN

        y += 6
        page.draw_line(fitz.Point(_MARGIN, y), fitz.Point(595 - _MARGIN, y),
                       color=(0.85, 0.87, 0.90), width=0.7)
        y += 18
        y = _cert_line(page, y, "History", size=11, bold=True)
        y += 6
        for ev in events:
            if y > 780:
                page = doc.new_page(width=595, height=842); y = _MARGIN
            line = f"{ev.get('at','')}   {ev.get('type','')}"
            if ev.get("actor"):
                line += f"   {ev['actor']}"
            y = _cert_line(page, y, line, size=8.2, color=(0.34, 0.38, 0.45),
                           font="cour")
        out = doc.tobytes(deflate=True, garbage=3)
    finally:
        doc.close()
    return out


def _cert_line(page: fitz.Page, y: float, text: str, *, size: float = 9.0,
               bold: bool = False, color=(0.06, 0.09, 0.20),
               font: str | None = None) -> float:
    """Write one line at `y` and return the y for the next one."""
    name = font or ("hebo" if bold else "helv")
    page.insert_text(fitz.Point(_MARGIN, y + size), text or "", fontname=name,
                     fontsize=size, color=color)
    return y + size + 4.0
