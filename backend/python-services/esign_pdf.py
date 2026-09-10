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
from typing import Any
from datetime import datetime, timezone
from typing import Any, Sequence

import fitz  # PyMuPDF

log = logging.getLogger("bdx.esign.pdf")

# --- the anchor vocabulary --------------------------------------------------
# {{<type>:<party kind>:<id>}} — the party half is the party_key stored on both
# the field and the recipient, so the token literally spells out who owns the
# box it marks.
# A party key may carry a SIGNER SLOT — `tenant:12#2` — for the second, third …
# person one organisation sends to sign. No slot means the first signer, which
# is the key every contract raised before this had: an old document re-read
# here finds exactly the boxes it always found.
ANCHOR_RE = re.compile(
    r"\{\{(signature|initial|name|title|date|text):(tenant|broker):(\d+(?:#\d+)?)\}\}")

FIELD_TYPES = ("signature", "initial", "name", "title", "date", "text")

# What separates the organisation from the person inside a party key.
SLOT_SEP = "#"


def slot_key(party_key: str, slot: int) -> str:
    """The key for one organisation's `slot`-th signer.

    Slot 1 is the base key unchanged — the person who has always signed there —
    so adding a second signatory never moves the first one's boxes.
    """
    return party_key if slot <= 1 else f"{party_key}{SLOT_SEP}{int(slot)}"


def base_key(party_key: str) -> str:
    """The ORGANISATION a key names, whichever of its people it points at.

    The access rule is still the whole key — a box belongs to one person — but
    "which side is this?" is answered by the base.
    """
    return party_key.split(SLOT_SEP, 1)[0]

# How big a box of each kind is, in points, measured from the anchor's top-left.
# A signature needs room for a drawn scrawl; a date does not.
#
# Everything except a signature is ONE ROW TALL. Initials are asked for beside
# a printed "Initials:" label, on a row of their own like "Full name" and "Date
# signed" above them — not above a ruled line. Given a taller box they were
# bottom-aligned into it (see `_fit`) and landed half an inch below their own
# label, out of line with every other row in the block. Only the signature,
# which really does sit above a rule, is allowed the extra height.
#
# And that height is the DISTANCE FROM ITS ANCHOR DOWN TO THE RULE, which both
# layouts leave about the same: the flowable block writes the anchor on the
# line above the rule (~25pt), and a placed block tags it 18pt above the line
# it then draws. At 42 the box overshot the rule by a third of an inch and
# landed on "Full name:" underneath — a signature written through the next
# label, and a clickable box covering a row it does not own. Anything taller
# than the gap has nowhere to go but into the row below.
BOX_SIZE: dict[str, tuple[float, float]] = {
    "signature": (185.0, 23.0),
    "initial":   (58.0, 15.0),
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

# ── what a signature BLOCK may contain ──────────────────────────────────────
#
# The block on a contract's signature page used to be four fixed lines written
# into contract_wording: signature, name, title, date, for both sides, on every
# contract ever raised. That is right for most of them and wrong for the rest —
# a treaty signed by two people who both hold the title already printed above
# the line does not need a title box, and a carrier who wants initials on the
# block cannot have them. Changing it meant changing this file.
#
# So the block is now DESCRIBED here and CHOSEN per contract. This list is the
# whole vocabulary: served to the form so the carrier ticks boxes, and read by
# the wording builder so what is ticked is what is drawn. Neither side holds a
# copy of it, which is the point — a field added here appears in the form and
# on the page with no other change anywhere.
#
#   key        one of FIELD_TYPES, so the anchor it writes is one
#              discover_fields already knows how to find
#   label      what the signer is asked for (from FIELD_LABEL — said once)
#   hint       why you would want it, for the person choosing
#   on         whether it is ticked when nobody has chosen yet
#   fixed      True for the one field a signature block cannot be without
SIGNATURE_BLOCK_FIELDS: tuple[dict, ...] = (
    {"key": "signature", "hint": "the signature itself, on the ruled line",
     "on": True, "fixed": True},
    {"key": "name", "hint": "printed underneath, so the scrawl can be read",
     "on": True, "fixed": False},
    {"key": "title", "hint": "the capacity they sign in",
     "on": True, "fixed": False},
    {"key": "date", "hint": "the day they signed — filled in for them",
     "on": True, "fixed": False},
    {"key": "initial", "hint": "initials as well as a signature",
     "on": False, "fixed": False},
)

# How the two blocks sit on the page. Also chosen rather than coded: side by
# side reads as one agreement between equals and fits on one page, stacked
# gives a long signatory list room to breathe.
SIGNATURE_ARRANGEMENTS: tuple[dict, ...] = (
    {"key": "side_by_side", "label": "Side by side",
     "hint": "the two blocks across the page — the usual arrangement"},
    {"key": "stacked", "label": "One above the other",
     "hint": "more room under each, for several signatories a side"},
    # The third answer to "where do the blocks go": anywhere. A contract that
    # has to be countersigned beside a particular clause, or that follows a
    # house layout the other two cannot express, is placed by hand — the blocks
    # are dragged onto the page and the document is drawn where they were left.
    {"key": "placed", "label": "Placed by hand",
     "hint": "drag each block onto the page where it should sit"},
)

# How big a placed block is, as a fraction of the page. SERVED, not restated in
# the browser: the ghost box somebody drags has to be the size of the block that
# gets drawn, or they are placing one thing and getting another.
#
# `width` is exact. `height` is the reserve the screen shows for ONE signatory
# — the drawn height follows how many lines that side asked for and how many
# people sign for it, so a side sending two grows downward from where the block
# was left. A block that would then run off the foot of the page is lifted so
# it fits, which is why the top-left corner is what is stored and not the box.
PLACED_BLOCK: dict[str, float] = {"width": 0.40, "height": 0.13}

# The two sides a block belongs to, in the words the wording prints. Named here
# so the form, the validator and the builder agree on the spelling.
SIGNATURE_SIDES = ("carrier", "counterparty")

DEFAULT_SIGNATURE_FIELDS = tuple(
    f["key"] for f in SIGNATURE_BLOCK_FIELDS if f["on"])
DEFAULT_ARRANGEMENT = SIGNATURE_ARRANGEMENTS[0]["key"]
_FIXED_FIELDS = tuple(f["key"] for f in SIGNATURE_BLOCK_FIELDS if f["fixed"])


class SignatureLayoutError(ValueError):
    """A block that could not be drawn as asked. Carries `errors` as
    {side: message} so the form can mark the column rather than print one
    sentence over both of them."""

    def __init__(self, message: str, errors: dict[str, str] | None = None):
        super().__init__(message)
        self.message = message
        self.errors = errors or {}


def signature_block_spec() -> dict:
    """The vocabulary, shaped for a form. Served, never restated client-side."""
    return {
        "fields": [{"key": f["key"], "label": FIELD_LABEL[f["key"]],
                    "hint": f["hint"], "default_on": f["on"], "fixed": f["fixed"]}
                   for f in SIGNATURE_BLOCK_FIELDS],
        "arrangements": [dict(a) for a in SIGNATURE_ARRANGEMENTS],
        "sides": list(SIGNATURE_SIDES),
        "default": default_signature_layout(),
        # The size of a hand-placed block, so the box dragged on the screen and
        # the box drawn on the page are one number.
        "placed_block": dict(PLACED_BLOCK),
    }


def default_signature_layout() -> dict:
    """What a contract that has never been asked gets. The four lines every
    contract raised before this was configurable already had, so an old row and
    a new one with nothing chosen produce the identical page."""
    return {"arrangement": DEFAULT_ARRANGEMENT,
            "fields": {side: list(DEFAULT_SIGNATURE_FIELDS)
                       for side in SIGNATURE_SIDES},
            # Nowhere placed by hand. Present so every reader gets the same
            # shape whether or not anybody ever dragged a block.
            "blocks": {}}


def normalise_signature_layout(raw: Any, *, strict: bool = False) -> dict:
    """A stored layout in the one shape everything downstream may assume.

    READ path (`strict=False`) never raises. Rows written before this existed
    hold `{}` or `{"arrangement": "stacked"}` and must keep rendering exactly as
    they did; anything unrecognisable falls back to the default rather than
    leaving a contract with no way to sign it, which is the worse failure by a
    long way.

    WRITE path (`strict=True`) refuses instead, because the moment to tell
    somebody their choice cannot be drawn is while they are making it.
    """
    src = raw if isinstance(raw, dict) else {}
    errors: dict[str, str] = {}

    arrangement = str(src.get("arrangement") or "").strip() or DEFAULT_ARRANGEMENT
    known_arrangements = {a["key"] for a in SIGNATURE_ARRANGEMENTS}
    if arrangement not in known_arrangements:
        if strict:
            errors["arrangement"] = (
                f"“{arrangement}” is not a way to arrange the blocks — "
                f"{' or '.join(sorted(known_arrangements))}.")
        arrangement = DEFAULT_ARRANGEMENT

    given = src.get("fields")
    given = given if isinstance(given, dict) else {}
    out: dict[str, list[str]] = {}
    for side in SIGNATURE_SIDES:
        wanted = given.get(side)
        if not isinstance(wanted, list):
            # Not "no fields" — not asked. A side left out of a layout that
            # names the other one is the form sending half of itself, and
            # silently giving that side an empty block would leave a contract
            # nobody can sign.
            out[side] = list(DEFAULT_SIGNATURE_FIELDS)
            continue
        # Deduplicated, and put back into the vocabulary's own order: the block
        # reads signature, name, title, date whatever order the boxes were
        # ticked in, and a form that let tick order change the page would make
        # two identical-looking choices produce different documents.
        chosen = {str(k).strip() for k in wanted if str(k).strip()}
        unknown = sorted(chosen - {f["key"] for f in SIGNATURE_BLOCK_FIELDS})
        if unknown and strict:
            errors[side] = f"no such line on a signature block: {', '.join(unknown)}"
        missing = [k for k in _FIXED_FIELDS if k not in chosen]
        if missing:
            if strict:
                errors[side] = (
                    "a signature block has to have somewhere to sign — "
                    f"{', '.join(FIELD_LABEL[k].lower() for k in missing)} "
                    "cannot be left off.")
            chosen |= set(missing)
        out[side] = [f["key"] for f in SIGNATURE_BLOCK_FIELDS if f["key"] in chosen]

    # ── where a hand-placed block sits ──────────────────────────────────────
    # Kept whatever the arrangement is, so switching to side-by-side to see how
    # it looks and back again does not throw away a placement somebody made.
    blocks: dict[str, dict] = {}
    given_blocks = src.get("blocks")
    given_blocks = given_blocks if isinstance(given_blocks, dict) else {}
    for side in SIGNATURE_SIDES:
        spot = given_blocks.get(side)
        if not isinstance(spot, dict):
            continue
        try:
            page = int(spot.get("page"))
            x = float(spot.get("x"))
            y = float(spot.get("y"))
        except (TypeError, ValueError):
            if strict:
                errors[side] = ("that block was not placed on the page — drag "
                                "it where it should sit")
            continue
        # A block whose top-left is off the page cannot be drawn anywhere
        # sensible. Refused on the way in rather than silently pulled back to
        # the margin, because "it moved when I saved it" is worse than "that is
        # off the page".
        if page < 1 or not (0.0 <= x <= 1.0) or not (0.0 <= y <= 1.0):
            if strict:
                errors[side] = "that block is off the page"
            continue
        blocks[side] = {"page": page, "x": round(x, 6), "y": round(y, 6)}

    if arrangement == "placed":
        missing_sides = [s for s in SIGNATURE_SIDES if s not in blocks]
        if missing_sides:
            if strict:
                for s in missing_sides:
                    errors.setdefault(
                        s, "drag this block onto the page before saving, or "
                           "choose one of the automatic arrangements")
            else:
                # READ path. A stored layout that asks to be placed by hand and
                # says nowhere would leave the contract with no signature block
                # at all, which is the one outcome worse than the wrong layout.
                arrangement = DEFAULT_ARRANGEMENT

    if strict and errors:
        raise SignatureLayoutError(
            "That signature block cannot be drawn as asked.", errors)
    return {"arrangement": arrangement, "fields": out, "blocks": blocks}


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


# ── drawing a signature block where somebody put it ────────────────────────
# Points, not fractions: these are distances DOWN one block, not positions on a
# page, so they do not scale with the paper. Named because the numbers appear
# twice — once to draw, once to measure — and two copies of 26 is how a rule
# ends up half a line above the space meant for the signature.
_BLK_TITLE_PT = 9.5
_BLK_LINE_PT = 8.5
_BLK_SIG_GAP = 30.0        # room above the rule for a signature to land in
_BLK_CAP_PT = 6.0          # air under the rule, where the audit caption goes
_BLK_ROW = 11.5            # one printed line under the rule


def placed_block_height(line_count: int | Sequence[int]) -> float:
    """How much room a block needs, in points.

    `line_count` is the lines under ONE rule, or a list with one entry per
    signatory in the block — a side that sends three people to sign is three
    rules tall, and measuring it as one is how the last two run off the paper.

    Generous on purpose: it counts the organisation line whether or not there
    is one, and leaves a few points under the last row. A block placed near the
    foot of a page is lifted so it fits, and over-measuring lifts it a little
    further than strictly necessary, where under-measuring would run the last
    line off the paper.
    """
    per = ([int(line_count)] if isinstance(line_count, int)
           else [int(n) for n in line_count] or [0])
    return (_BLK_TITLE_PT + 2 + _BLK_LINE_PT + 2
            + sum(_BLK_SIG_GAP + _BLK_CAP_PT + 6 + _BLK_ROW * max(0, n)
                  for n in per))


def draw_signature_blocks(pdf_bytes: bytes, blocks: Sequence[dict]) -> bytes:
    """Draw hand-placed signature blocks onto an already-composed document.

    WHY NOT IN THE FLOWABLES. The two automatic arrangements are laid out by
    ReportLab, which places things in reading order — that is what a flowable
    layout is for and why it cannot put a block at a point somebody chose. So a
    placed block is drawn afterwards, onto the finished page, in the same way a
    signature is stamped onto it later: coordinates are fractions of the page,
    top-left origin, exactly as everywhere else in this module.

    Each block is `{"page", "x", "y", "title", "org", "lines", "anchors"}`,
    where `lines` are the printed labels under the rule and `anchors` maps a
    field type to the party key that owns it, so the signing round finds its
    boxes here the same way it finds them anywhere else. Anchors are written at
    render mode 3 — genuinely invisible, unlike the white text a flowable is
    limited to.

    A block whose page is past the end of a document that got shorter is drawn
    on the LAST page rather than dropped: a contract with nowhere to sign is the
    worse failure, and it is silent.
    """
    if not blocks:
        return pdf_bytes
    doc = _open(pdf_bytes)
    try:
        for b in blocks:
            page_no = max(1, min(int(b.get("page") or 1), doc.page_count))
            if page_no != b.get("page"):
                log.warning("[contract] signature block for %s was placed on "
                            "page %s of a %s-page document — drawn on the last "
                            "page", b.get("title"), b.get("page"),
                            doc.page_count)
            page = doc[page_no - 1]
            pw, ph = page.rect.width, page.rect.height
            # One entry per SIGNATORY in this block, each {"lines", "anchors"}.
            # A block that names nobody in particular is one signatory with no
            # anchors, which is what `lines`/`anchors` on the block itself mean
            # — the older shape, still accepted so a caller that has only ever
            # drawn one rule needs no change.
            groups: list[dict] = list(b.get("signers") or [])
            if not groups:
                groups = [{"lines": b.get("lines") or [],
                           "anchors": b.get("anchors") or {}}]
            width = float(b.get("width") or PLACED_BLOCK["width"]) * pw
            height = placed_block_height(
                [len(g.get("lines") or []) for g in groups])

            x0 = max(0.0, min(float(b.get("x") or 0.0) * pw, pw - width))
            y0 = max(0.0, min(float(b.get("y") or 0.0) * ph, ph - height))

            def tag(kind: str, at: tuple[float, float],
                    keys: dict[str, str]) -> None:
                """The invisible token that puts a signing box right here."""
                key = keys.get(kind)
                if not key:
                    return
                page.insert_text(fitz.Point(*at), anchor_token(kind, key),
                                 fontsize=5, render_mode=3)

            y = y0 + _BLK_TITLE_PT
            page.insert_text(fitz.Point(x0, y), str(b.get("title") or ""),
                             fontname="hebo", fontsize=_BLK_TITLE_PT - 0.5,
                             color=(0.06, 0.09, 0.20))
            if b.get("org"):
                y += _BLK_LINE_PT + 2
                page.insert_text(fitz.Point(x0, y), str(b["org"]),
                                 fontname="helv", fontsize=_BLK_LINE_PT - 0.5,
                                 color=(0.06, 0.09, 0.20))

            for g in groups:
                # Each is {"label", "value", "type"}: what is printed, what is
                # already known (a named signer's own name), and which kind of
                # box goes there when it is not.
                lines: list[dict] = list(g.get("lines") or [])
                keys: dict[str, str] = dict(g.get("anchors") or {})

                # The signature lands ON the rule, so its anchor sits in the
                # space above it — the same relationship the flowable layout
                # draws.
                tag("signature", (x0, y + 12), keys)
                y += _BLK_SIG_GAP
                page.draw_line(fitz.Point(x0, y), fitz.Point(x0 + width, y),
                               color=(0.45, 0.48, 0.55), width=0.7)
                y += _BLK_CAP_PT     # the audit caption sits in here

                # TWO COLUMNS, not one run of text. Every label is printed at
                # x0 and everything beside it starts at the same x — measured
                # off the widest label this block actually prints. Started at
                # the end of its own label, "Initials:" and "Date signed:" put
                # their boxes nearly half an inch apart, which is what makes a
                # signature block look thrown at the page rather than set on it.
                label_w = max(
                    [fitz.get_text_length(r["label"], "helv", _BLK_LINE_PT - 1)
                     for r in lines] or [0.0]) + 4
                for row in lines:
                    y += _BLK_ROW
                    page.insert_text(fitz.Point(x0, y), row["label"],
                                     fontname="helv", fontsize=_BLK_LINE_PT - 1,
                                     color=(0.35, 0.38, 0.45))
                    after = x0 + label_w
                    if row.get("value"):
                        page.insert_text(fitz.Point(after, y), row["value"],
                                         fontname="helv",
                                         fontsize=_BLK_LINE_PT - 1,
                                         color=(0.06, 0.09, 0.20))
                    elif row.get("type"):
                        tag(row["type"], (after, y), keys)
                y += 6                   # air before the next signatory's rule
        out = doc.tobytes(deflate=True, garbage=3)
    finally:
        doc.close()
    return out


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
            page.insert_image(_fit(rect, img, on_rule=st.type == "signature"),
                              stream=img, keep_proportion=True, overlay=True)
            drawn = True
        except Exception as e:                     # a corrupt PNG must not stop a signing
            log.warning("[esign] drawn signature could not be placed (%s) — "
                        "falling back to the typed name", e)
    if not drawn:
        text = (st.value or "").strip()
        if text:
            # Times-Italic ("tiit") reads as a signature where Helvetica reads
            # as a form field. Shrink to fit rather than overflow the block —
            # by HEIGHT as well as width, because initials are set on a
            # one-row box and a 20pt letter in a 15pt row either drops below
            # its label or does not fit at all and prints nothing.
            size = min(20.0, rect.height - 2.0)
            while size > 8.0 and fitz.get_text_length(text, "tiit", size) > rect.width - 6:
                size -= 1.0
            page.insert_textbox(rect, text, fontname="tiit", fontsize=size,
                                color=(0.06, 0.09, 0.20), align=fitz.TEXT_ALIGN_LEFT)
    if st.caption:
        # UNDER the ruled line. The box now ends ON the rule and a signature
        # fills it, so the old position — the bottom of the box — printed the
        # audit line straight through the signature it describes. Below the
        # rule is where a reader looks for it anyway, and both layouts keep
        # room there for exactly this.
        cap = fitz.Rect(rect.x0, rect.y1 + 0.5,
                        rect.x0 + max(rect.width, 190), rect.y1 + 8)
        page.insert_textbox(cap, st.caption, fontname="helv", fontsize=5.6,
                            color=(0.45, 0.48, 0.55))


def _fit(box: fitz.Rect, image: bytes, *, on_rule: bool = True) -> fitz.Rect:
    """Where a drawn signature actually goes inside its box.

    The fit is computed here rather than left to `keep_proportion`, for two
    reasons. It stretches a squarish scrawl to fill a wide box — measurably, on
    this version — which turns a signature into a smear. And even when it does
    scale correctly it centres the result, whereas a signature belongs sitting
    on the ruled line at the left, exactly where a pen would have left it.

    `on_rule` is what the box sits on. A signature sits on a ruled line and
    hangs from the bottom of its box. INITIALS do not — they are asked for
    beside a printed label, on a row with "Full name" and "Date signed", and a
    scrawl narrower than its row hung from the bottom sits lower than the words
    either side of it. Those start at the top of the row, so initials do too.

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
    # Left-aligned always. Vertically: on the rule for a signature, on the top
    # of the row for anything else.
    if on_rule:
        return fitz.Rect(box.x0, box.y1 - h, box.x0 + w, box.y1)
    return fitz.Rect(box.x0, box.y0, box.x0 + w, box.y0 + h)


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
