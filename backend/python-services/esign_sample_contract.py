"""The sample contract Create-a-Contract step 4 is demonstrated on.

Steps 1–3 of the wizard (Terms, Wording, Read it through) produce the document
that comes here. Until they do, this module writes one that is shaped exactly
like theirs: a binding authority agreement over four pages, the agreed limits as
a schedule, and an execution page carrying the anchors that step 4 reads.

WHAT MAKES IT A SAMPLE AND NOT A MOCK
-------------------------------------
The anchors. Every signature block is written with an INVISIBLE token beside it
naming the organisation that owns the block:

    {{signature:tenant:12}}     the insurer's block   (carrier tenant_id 12)
    {{signature:broker:47}}     the broker's block    (broker_party_id 47)

`esign_pdf.discover_fields()` finds those, turns each into a rectangle, and
reads the owner out of the token. So the question "is this the broker's box or
the carrier's?" is settled by the document itself, and any PDF that carries the
same tokens — including the real one step 2 will generate — drops into step 4
with no change to a line of this file.

Replacing this with the real document is therefore one substitution: hand
`build_envelope_pdf()` the wording that step 2 produced with the same anchors in
its execution block, or pass the finished PDF straight to the envelope and skip
this module entirely.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from datetime import date

import fitz  # PyMuPDF

from esign_pdf import anchor_token

# --- page furniture ---------------------------------------------------------
PAGE_W, PAGE_H = 595.0, 842.0          # A4 in points
MARGIN = 62.0
CONTENT_W = PAGE_W - 2 * MARGIN

INK = (0.055, 0.075, 0.125)
MUTED = (0.34, 0.38, 0.45)
SOFT = (0.58, 0.61, 0.67)
RULE = (0.84, 0.86, 0.89)
BRAND = (0.027, 0.447, 0.510)          # the platform teal, #077282


@dataclass
class Limit:
    """One agreed limit — a line of the schedule, and later a check that runs on
    every file the broker sends."""
    what: str
    value: str
    on_breach: str = "Stop the row"


@dataclass
class ContractTerms:
    """Everything the document says, gathered in one place.

    The wizard's step 1 collects exactly these. Keeping them in a dataclass
    rather than a dict is what lets the real step 1 hand its answers over
    without this module guessing at key names.
    """
    title: str = "Schedule A — 2027"
    carrier_name: str = "Insurisk Specialty"
    carrier_party_key: str = "tenant:1"
    carrier_signer: str = "Dana Alvarez"
    carrier_signer_title: str = "Carrier Admin"
    broker_name: str = "CRC Insurisk"
    broker_party_key: str = "broker:1"
    broker_signer: str = "Marco Vance"
    broker_signer_title: str = "Broker Admin"
    programme: str = "Spectrum Transportation"
    reference: str = "SCH-A"
    starts: date = date(2027, 1, 1)
    ends: date = date(2027, 12, 31)
    currency: str = "USD"
    limits: list[Limit] = dc_field(default_factory=lambda: [
        Limit("Most commission the carrier will pay", "15% of premium on each risk"),
        Limit("Smallest premium the carrier will take", "USD 500 per policy"),
        Limit("Biggest risk the carrier will cover", "USD 5,000,000 any one risk"),
        Limit("Most premium for the whole term", "USD 12,000,000", "Just flag it"),
        Limit("How late a bordereau may be", "15 days after month end", "Just flag it"),
    ])
    initials_every_page: bool = False

    @property
    def term(self) -> str:
        return f"{self.starts:%d %B %Y} to {self.ends:%d %B %Y}"


# --- a very small layout engine --------------------------------------------
class _Doc:
    """A cursor that writes down an A4 page and starts a new one when it runs
    out of room. Enough for a contract; deliberately not a typesetter."""

    def __init__(self, terms: ContractTerms):
        self.terms = terms
        self.doc = fitz.open()
        self.page: fitz.Page | None = None
        self.y = 0.0
        self.page_no = 0
        self.new_page()

    def new_page(self) -> None:
        self.page = self.doc.new_page(width=PAGE_W, height=PAGE_H)
        self.page_no += 1
        self.y = MARGIN
        self._running_head()

    def _running_head(self) -> None:
        if self.page_no == 1:
            return
        self.page.insert_text(
            fitz.Point(MARGIN, MARGIN - 18),
            f"{self.terms.title} · {self.terms.carrier_name} and {self.terms.broker_name}",
            fontname="helv", fontsize=7.5, color=SOFT)
        self.page.draw_line(fitz.Point(MARGIN, MARGIN - 12),
                            fitz.Point(PAGE_W - MARGIN, MARGIN - 12),
                            color=RULE, width=0.6)
        self.y = MARGIN + 6

    def room(self, needed: float) -> None:
        """Start a new page unless `needed` points are still free."""
        if self.y + needed > PAGE_H - MARGIN - 30:
            self.new_page()

    def para(self, text: str, *, size: float = 9.6, leading: float = 1.5,
             color=INK, font: str = "helv", gap: float = 9.0,
             indent: float = 0.0) -> None:
        rect = fitz.Rect(MARGIN + indent, self.y, PAGE_W - MARGIN, PAGE_H - MARGIN)
        left = self.page.insert_textbox(rect, text, fontname=font, fontsize=size,
                                        lineheight=leading, color=color,
                                        align=fitz.TEXT_ALIGN_LEFT)
        if left < 0:                       # did not fit — retry on a fresh page
            self.new_page()
            rect = fitz.Rect(MARGIN + indent, self.y, PAGE_W - MARGIN, PAGE_H - MARGIN)
            left = self.page.insert_textbox(rect, text, fontname=font, fontsize=size,
                                            lineheight=leading, color=color,
                                            align=fitz.TEXT_ALIGN_LEFT)
        self.y = PAGE_H - MARGIN - max(left, 0.0) + gap

    def heading(self, text: str, *, size: float = 11.5, gap: float = 7.0) -> None:
        self.room(46)
        self.page.insert_text(fitz.Point(MARGIN, self.y + size), text,
                              fontname="hebo", fontsize=size, color=INK)
        self.y += size + gap

    def clause(self, number: str, title: str, body: str) -> None:
        self.room(70)
        self.page.insert_text(fitz.Point(MARGIN, self.y + 9.6), number,
                              fontname="hebo", fontsize=9.6, color=BRAND)
        self.page.insert_text(fitz.Point(MARGIN + 30, self.y + 9.6), title,
                              fontname="hebo", fontsize=9.6, color=INK)
        self.y += 15
        self.para(body, indent=30)

    def rule(self, gap_before: float = 4.0, gap_after: float = 12.0) -> None:
        self.y += gap_before
        self.page.draw_line(fitz.Point(MARGIN, self.y), fitz.Point(PAGE_W - MARGIN, self.y),
                            color=RULE, width=0.7)
        self.y += gap_after

    def anchor(self, field_type: str, party_key: str, x: float, y: float) -> None:
        """Drop an invisible tagging token at an exact point on this page.

        render_mode 3 draws nothing at all: the token is in the text layer where
        `search_for` finds it, and on no printout anywhere. `insert_text` places
        the BASELINE at y, and the rect the search returns starts a little above
        that — which is why blocks below are laid out from the ruled line, not
        from this call.
        """
        self.page.insert_text(fitz.Point(x, y), anchor_token(field_type, party_key),
                              fontname="helv", fontsize=7, render_mode=3)


# --- the document -----------------------------------------------------------
def build_sample_contract(terms: ContractTerms | None = None) -> bytes:
    """The sample contract as PDF bytes, anchors in place."""
    t = terms or ContractTerms()
    d = _Doc(t)

    _cover(d, t)
    _recitals(d, t)
    _schedule(d, t)
    _conditions(d, t)
    _execution(d, t)

    if t.initials_every_page:
        _initials_footers(d, t)

    out = d.doc.tobytes(deflate=True, garbage=3)
    d.doc.close()
    return out


def _cover(d: _Doc, t: ContractTerms) -> None:
    p = d.page
    p.draw_rect(fitz.Rect(MARGIN, MARGIN, MARGIN + 34, MARGIN + 4),
                color=BRAND, fill=BRAND)
    d.y = MARGIN + 24
    p.insert_text(fitz.Point(MARGIN, d.y + 9), "KAVACHIO · BORDEREAU PLATFORM",
                  fontname="hebo", fontsize=7.6, color=SOFT)
    d.y += 30
    p.insert_text(fitz.Point(MARGIN, d.y + 22), "Binding Authority Agreement",
                  fontname="hebo", fontsize=22, color=INK)
    d.y += 34
    p.insert_text(fitz.Point(MARGIN, d.y + 13), t.title,
                  fontname="helv", fontsize=13, color=MUTED)
    d.y += 30
    d.rule(gap_after=18)

    for k, v in (
        ("Programme", t.programme),
        ("Insurer", t.carrier_name),
        ("Broker", t.broker_name),
        ("Term", t.term),
        ("Currency", t.currency),
        ("Reference", t.reference),
    ):
        d.page.insert_text(fitz.Point(MARGIN, d.y + 9.6), k,
                           fontname="helv", fontsize=9.6, color=SOFT)
        d.page.insert_text(fitz.Point(MARGIN + 120, d.y + 9.6), v,
                           fontname="hebo", fontsize=9.6, color=INK)
        d.y += 19

    d.y += 8
    d.rule(gap_after=16)
    d.para(
        "This agreement is made between the Insurer and the Broker named above. "
        "It sets out the business the Broker may bind on the Insurer's behalf, the "
        "limits that business must stay inside, and how the Broker reports what it "
        "has written. Every limit in the Schedule is also a check: it runs on each "
        "bordereau the Broker sends, and a row that breaks it is stopped or flagged "
        "as the Schedule says.",
        color=MUTED)


def _recitals(d: _Doc, t: ContractTerms) -> None:
    d.new_page()
    d.heading("1 · The agreement")
    d.clause("1.1", "Authority granted",
             f"The Insurer authorises the Broker to bind risks under the {t.programme} "
             f"programme between {t.starts:%d %B %Y} and {t.ends:%d %B %Y}, in the "
             f"classes and territories set out in the Schedule, and on no other basis.")
    d.clause("1.2", "Limits are binding",
             "The Broker may not bind a risk that breaks a limit in the Schedule. A "
             "risk bound outside those limits is not covered by this agreement, and "
             "the Insurer may decline it whatever the bordereau says.")
    d.clause("1.3", "Term",
             f"This agreement runs from {t.starts:%d %B %Y} to {t.ends:%d %B %Y}. "
             "Risks bound during the term stay covered to their own expiry, even "
             "where that falls after the term ends.")
    d.clause("1.4", "Changes",
             "Nothing in this agreement changes unless both parties sign an "
             "endorsement saying so. An endorsement changes only the clauses it "
             "names; every other clause carries on unchanged, and bordereau rows "
             "before the day it takes effect are still checked against the old wording.")


def _schedule(d: _Doc, t: ContractTerms) -> None:
    d.new_page()
    d.heading("2 · The Schedule of limits")
    d.para("Each line is a term of this agreement and a check that runs on every "
           "file the Broker sends. The last column says what happens to a row that "
           "breaks it.", color=MUTED, size=9.2)

    head_y = d.y
    d.page.draw_rect(fitz.Rect(MARGIN, head_y, PAGE_W - MARGIN, head_y + 20),
                     color=None, fill=(0.965, 0.972, 0.980))
    for label, dx in (("WHAT WAS AGREED", 8), ("THE LIMIT", 250), ("IF A FILE BREAKS IT", 390)):
        d.page.insert_text(fitz.Point(MARGIN + dx, head_y + 13.5), label,
                           fontname="hebo", fontsize=7.2, color=SOFT)
    d.y = head_y + 20

    for i, lim in enumerate(t.limits):
        d.room(34)
        row_y = d.y
        if i % 2:
            d.page.draw_rect(fitz.Rect(MARGIN, row_y, PAGE_W - MARGIN, row_y + 26),
                             color=None, fill=(0.984, 0.988, 0.992))
        d.page.insert_textbox(fitz.Rect(MARGIN + 8, row_y + 5, MARGIN + 244, row_y + 26),
                              lim.what, fontname="helv", fontsize=8.8, color=INK)
        d.page.insert_textbox(fitz.Rect(MARGIN + 250, row_y + 5, MARGIN + 384, row_y + 26),
                              lim.value, fontname="hebo", fontsize=8.8, color=INK)
        d.page.insert_textbox(fitz.Rect(MARGIN + 390, row_y + 5, PAGE_W - MARGIN, row_y + 26),
                              lim.on_breach, fontname="helv", fontsize=8.8, color=MUTED)
        d.page.draw_line(fitz.Point(MARGIN, row_y + 26), fitz.Point(PAGE_W - MARGIN, row_y + 26),
                         color=RULE, width=0.5)
        d.y = row_y + 26

    d.y += 18
    d.clause("2.1", "Reporting",
             "The Broker sends a bordereau for each month within fifteen days of "
             "the month ending, in the agreed format, covering every risk bound in "
             "that month whether or not premium has been collected.")
    d.clause("2.2", "Money",
             f"Premium is accounted in {t.currency}. Commission is deducted at the "
             "rate in the Schedule and no other deduction is made without the "
             "Insurer agreeing to it in writing first.")


def _conditions(d: _Doc, t: ContractTerms) -> None:
    d.new_page()
    d.heading("3 · General conditions")
    d.clause("3.1", "Records",
             "The Broker keeps a full record of every risk bound under this "
             "agreement and lets the Insurer inspect it on reasonable notice, "
             "during the term and for seven years after it ends.")
    d.clause("3.2", "Ending it early",
             "Either party may end this agreement on ninety days' written notice. "
             "Risks already bound stay covered to their own expiry.")
    d.clause("3.3", "Disagreements",
             "If the parties disagree about what this agreement means, they will "
             "try to settle it between themselves before either of them starts "
             "proceedings.")
    d.clause("3.4", "Signing",
             "This agreement may be signed electronically and in counterparts. An "
             "electronic signature applied through the Insurer's platform binds "
             "the party who applied it exactly as a wet signature would, and the "
             "platform records who signed, when, and from where.")


def _execution(d: _Doc, t: ContractTerms) -> None:
    """The execution page — the one that matters for step 4.

    Two blocks, side by side, each carrying invisible anchors that name the
    organisation owning it. The left block is the Insurer's and no broker link
    can fill it; the right block is the Broker's and no insurer link can. That
    is not a rule the page enforces — it is a rule the SERVER enforces, and this
    page is simply where it is written down.
    """
    d.new_page()
    d.heading("4 · Signatures")
    d.para("Signed by the persons named below, each for and on behalf of the "
           "organisation shown. The Insurer signs first; the Broker signs the same "
           "document afterwards, with the Insurer's signature already on it.",
           color=MUTED, size=9.2)
    d.y += 10

    col_w = (CONTENT_W - 34) / 2
    left_x = MARGIN
    right_x = MARGIN + col_w + 34
    top = d.y

    _signature_block(d, left_x, top, col_w,
                     party_key=t.carrier_party_key,
                     heading="For and on behalf of the Insurer",
                     org=t.carrier_name,
                     signer=t.carrier_signer,
                     signer_title=t.carrier_signer_title,
                     order_note="Signs first")
    _signature_block(d, right_x, top, col_w,
                     party_key=t.broker_party_key,
                     heading="For and on behalf of the Broker",
                     org=t.broker_name,
                     signer=t.broker_signer,
                     signer_title=t.broker_signer_title,
                     order_note="Signs second")

    d.y = top + 214
    d.rule(gap_after=14)
    d.para(
        "Neither signature is a personal undertaking. Each binds the organisation "
        "named above it, and the platform records the name, the role, the time and "
        "the device alongside it. A completed copy is delivered to both parties, "
        "sealed so that any later alteration to it can be detected.",
        color=SOFT, size=8.4)


def _signature_block(d: _Doc, x: float, y: float, w: float, *, party_key: str,
                     heading: str, org: str, signer: str, signer_title: str,
                     order_note: str) -> None:
    p = d.page
    p.insert_text(fitz.Point(x, y + 8), heading.upper(),
                  fontname="hebo", fontsize=7.0, color=SOFT)
    p.insert_text(fitz.Point(x, y + 25), org, fontname="hebo", fontsize=11, color=INK)
    p.insert_text(fitz.Point(x, y + 39), order_note, fontname="helv", fontsize=7.4, color=BRAND)

    # --- signature -----------------------------------------------------------
    sig_line_y = y + 92
    p.draw_line(fitz.Point(x, sig_line_y), fitz.Point(x + w, sig_line_y),
                color=(0.72, 0.75, 0.79), width=0.8)
    p.insert_text(fitz.Point(x, sig_line_y + 11), "Signature",
                  fontname="helv", fontsize=7.4, color=SOFT)
    # The anchor sits where the TOP of the signature box goes: the box is 42pt
    # tall, so anchoring 44pt above the rule leaves the scrawl sitting on it.
    d.anchor("signature", party_key, x, sig_line_y - 44)

    # --- name ---------------------------------------------------------------
    name_line_y = y + 132
    p.draw_line(fitz.Point(x, name_line_y), fitz.Point(x + w, name_line_y),
                color=(0.82, 0.85, 0.88), width=0.7)
    p.insert_text(fitz.Point(x, name_line_y + 11), f"Name  (expected: {signer})",
                  fontname="helv", fontsize=7.4, color=SOFT)
    d.anchor("name", party_key, x, name_line_y - 4)

    # --- title --------------------------------------------------------------
    title_line_y = y + 164
    p.draw_line(fitz.Point(x, title_line_y), fitz.Point(x + w, title_line_y),
                color=(0.82, 0.85, 0.88), width=0.7)
    p.insert_text(fitz.Point(x, title_line_y + 11), f"Title  (expected: {signer_title})",
                  fontname="helv", fontsize=7.4, color=SOFT)
    d.anchor("title", party_key, x, title_line_y - 4)

    # --- date ---------------------------------------------------------------
    date_line_y = y + 196
    p.draw_line(fitz.Point(x, date_line_y), fitz.Point(x + w * 0.62, date_line_y),
                color=(0.82, 0.85, 0.88), width=0.7)
    p.insert_text(fitz.Point(x, date_line_y + 11), "Date signed",
                  fontname="helv", fontsize=7.4, color=SOFT)
    d.anchor("date", party_key, x, date_line_y - 4)


def _initials_footers(d: _Doc, t: ContractTerms) -> None:
    """Initials at the foot of every page but the last.

    Off by default. Most contracts never need it — it is here for the times a
    broker's lawyers insist, which is exactly the choice the Signatures screen
    offers.
    """
    for i in range(d.doc.page_count - 1):
        page = d.doc[i]
        y = PAGE_H - MARGIN + 6
        page.insert_text(fitz.Point(MARGIN, y), "Insurer initials",
                         fontname="helv", fontsize=6.4, color=SOFT)
        page.insert_text(fitz.Point(MARGIN, y - 4),
                         anchor_token("initial", t.carrier_party_key),
                         fontname="helv", fontsize=6, render_mode=3)
        page.insert_text(fitz.Point(PAGE_W / 2 + 30, y), "Broker initials",
                         fontname="helv", fontsize=6.4, color=SOFT)
        page.insert_text(fitz.Point(PAGE_W / 2 + 30, y - 4),
                         anchor_token("initial", t.broker_party_key),
                         fontname="helv", fontsize=6, render_mode=3)
