"""A signature has to reach the PAGE, not just the signer's screen.

The signing screen draws YOUR OWN values over the page image in HTML, and
draws nothing over the other party's — a box that is not yours and is filled
renders empty on the stated grounds that its value is already burned into the
page underneath. So anything that fails to stamp is invisible to exactly one
person: the one who needs to see it. The signer's own screen looks correct
throughout.

That is what happened to every typed ("Type it") signature. `insert_textbox`
writes nothing at all and returns a negative number when one line of the
requested size does not fit the box by HEIGHT, the sizing loop measured width
only, and a signature box is deliberately no taller than the gap above its
rule. The carrier signed, saw their name, and the broker opened a contract
with an empty signature line.

Self-contained: builds its own blank PDF, touches no database.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")

import fitz
import pytest

from esign_pdf import BOX_SIZE, Stamp, stamp_fields

W, H = 595.0, 842.0
CAPTION = "Signed by sayaj · 27 Sep 2026 · 10.0.0.4"


def blank_page() -> bytes:
    doc = fitz.open()
    doc.new_page(width=W, height=H)
    out = doc.tobytes()
    doc.close()
    return out


def box(kind: str, top: float) -> dict:
    """A field box the size the app actually builds for that kind."""
    w, h = BOX_SIZE[kind]
    return {"x": 72 / W, "y": top / H, "w": w / W, "h": h / H}


def page_text(pdf: bytes) -> str:
    doc = fitz.open(stream=pdf, filetype="pdf")
    text = doc[0].get_text()
    doc.close()
    return text


# Names chosen for their shapes: descenders are what collide with the rule and
# the caption beneath it, and a long one is what drives the size down.
TYPED = ["sayaj", "Tom", "Priya Sharma", "Bartholomew Fotheringay-Smythe"]


@pytest.mark.parametrize("name", TYPED)
def test_a_typed_signature_is_stamped_onto_the_page(name):
    pdf = stamp_fields(blank_page(), [
        Stamp(page=1, type="signature", value=name, image=None,
              caption=CAPTION, **box("signature", 300)),
    ])
    assert name in page_text(pdf)


def test_typed_initials_are_stamped_onto_the_page():
    """An initials box is shorter than a signature box, so it fails the same
    way for the same reason and has to be covered separately."""
    pdf = stamp_fields(blank_page(), [
        Stamp(page=1, type="initial", value="SJ", image=None,
              **box("initial", 300)),
    ])
    assert "SJ" in page_text(pdf)


def test_the_audit_caption_is_stamped():
    """Who signed and when. It goes in the few points of air under the rule,
    which is less than its own line box — so it never printed, on drawn
    signatures as well as typed ones."""
    pdf = stamp_fields(blank_page(), [
        Stamp(page=1, type="signature", value="sayaj", caption=CAPTION,
              **box("signature", 300)),
    ])
    assert CAPTION in page_text(pdf)


@pytest.mark.parametrize("kind,top", [("signature", 300), ("initial", 300),
                                      ("name", 300), ("date", 300)])
def test_the_mark_stays_inside_its_own_box(kind, top):
    """Fitting it on the page is half the job; landing it in the right place is
    the other half. A signature that overruns downward runs through the audit
    caption, and initials that hang from the bottom of their row sit below the
    label printed beside them."""
    value = "sayaj"
    pdf = stamp_fields(blank_page(), [
        Stamp(page=1, type=kind, value=value, **box(kind, top)),
    ])
    doc = fitz.open(stream=pdf, filetype="pdf")
    blocks = [b for b in doc[0].get_text("blocks") if value in b[4]]
    doc.close()
    assert blocks, f"{kind} was not stamped at all"
    _x0, y0, _x1, y1, *_ = blocks[0]
    height = BOX_SIZE[kind][1]
    assert y0 >= top - 1.0, f"{kind} starts above its box"
    assert y1 <= top + height + 1.0, f"{kind} runs past the bottom of its box"


def test_a_drawn_signature_still_wins_over_the_typed_name():
    """The typed name is the FALLBACK. When a scrawl was drawn or uploaded it
    is the signature, and the name must not be printed under it as well."""
    canvas = fitz.open()
    page = canvas.new_page(width=40, height=20)
    page.draw_line(fitz.Point(2, 14), fitz.Point(38, 14), width=2)
    png = page.get_pixmap().tobytes("png")
    canvas.close()

    import base64
    url = "data:image/png;base64," + base64.b64encode(png).decode()
    pdf = stamp_fields(blank_page(), [
        Stamp(page=1, type="signature", value="sayaj", image=url,
              **box("signature", 300)),
    ])
    doc = fitz.open(stream=pdf, filetype="pdf")
    images = len(doc[0].get_images())
    text = doc[0].get_text()
    doc.close()
    assert images == 1, "the drawn signature was not placed"
    assert "sayaj" not in text, "the typed name was printed under the drawing"


def test_a_box_too_small_degrades_to_smaller_type_not_to_silence():
    """The general rule behind the bug: never let a value fail to print. A box
    tighter than anything the app builds today should still get its text."""
    w, _h = BOX_SIZE["name"]
    pdf = stamp_fields(blank_page(), [
        Stamp(page=1, type="name", value="sayaj",
              x=72 / W, y=300 / H, w=w / W, h=6 / H),
    ])
    assert "sayaj" in page_text(pdf)


def test_nothing_is_stamped_for_an_empty_value():
    """A box left empty is NOT SIGNED, and must stay visibly empty."""
    pdf = stamp_fields(blank_page(), [
        Stamp(page=1, type="signature", value="", image=None,
              **box("signature", 300)),
    ])
    assert page_text(pdf).strip() == ""
