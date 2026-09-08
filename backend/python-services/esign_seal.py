"""Sealing the finished contract — a real digital signature over the PDF.

WHAT THIS IS
------------
When everybody has signed, the file gets one cryptographic signature applied
across the whole document, using an X.509 certificate belonging to Kavachio.
Adobe Reader then opens it with a blue banner naming the certificate holder,
and — because the seal is a CERTIFICATION signature with NO_CHANGES — turns it
red the moment a single byte of any page is altered.

Until now the drawn scrawl and the typed name were the only evidence that a
page had not been edited afterwards, and neither is evidence at all: both are
pixels, and pixels can be moved. The seal is what makes "this is the document
they agreed to" checkable by a stranger, in a reader they already trust,
without asking Kavachio anything.

YOU DO NOT NEED DOCUSIGN FOR THIS
---------------------------------
DocuSign is a competing product, not a verification authority — buying it would
replace this feature, not enable it. What produces the green tick in a PDF
reader is a certificate issued by a CA in the reader's trust store. DocuSign's
own PDFs are sealed exactly this way, with DocuSign's certificate.

WHOSE SIGNATURE THIS IS — READ THIS BEFORE QUOTING IT TO ANYONE
---------------------------------------------------------------
The seal is the PLATFORM's, not the signer's. It attests:

    "Kavachio produced this document, and it has not changed since."

It does NOT cryptographically attest "Dana Alvarez personally signed this" —
that would need a certificate issued to Dana, on a token in Dana's possession,
which is what a qualified signature (eIDAS QES, or an Indian DSC) means and
what makes those expensive and slow to roll out. What ties the signature to
Dana here is the evidence chain in contract_esign_event: a one-time code proved
control of the mailbox, and the IP, user agent and timestamps were recorded at
the moment of signing. That is the same standard of proof DocuSign, Adobe Sign
and HelloSign sell as an electronic signature, and it is what almost every
commercial contract is signed with.

So: the seal makes the document tamper-evident. The audit trail makes it
attributable. Neither does the other's job.

CONFIGURING IT
--------------
    ESIGN_SEAL_P12       path to a PKCS#12 bundle (.p12/.pfx: key + cert chain)
    ESIGN_SEAL_P12_PASS  its passphrase
    ESIGN_SEAL_REASON    optional, shown in the reader's signature panel
    ESIGN_SEAL_LOCATION  optional, likewise
    ESIGN_SEAL_TSA_URL   optional RFC 3161 timestamp authority

With no certificate configured this module does NOTHING and says so once. That
is deliberate: a missing certificate must never stop a contract completing.
Signatures that genuinely happened are not rolled back because an operator has
not bought a certificate yet, and an unsealed PDF is exactly what the product
produced last week.

THE TIMESTAMP, AND WHY IT MATTERS MORE THAN IT LOOKS
----------------------------------------------------
Without a TSA the signature carries the SIGNING MACHINE's clock, which proves
nothing to a sceptic and — worse — stops verifying the day the certificate
expires, because a reader can no longer tell whether the signature was made
while the certificate was valid. A TSA timestamp is countersigned by a third
party and keeps the document verifiable long after the certificate lapses.
Contracts outlive certificates, so for anything that has to stand up in a
dispute years later, set ESIGN_SEAL_TSA_URL.
"""

from __future__ import annotations

import logging
import os
from io import BytesIO
from typing import Any

log = logging.getLogger(__name__)

# Named so the reader's signature panel says something a human recognises
# rather than "Signature1".
FIELD_NAME = "KavachioSeal"

_warned = False


def _cfg() -> dict[str, str]:
    return {
        "p12": (os.getenv("ESIGN_SEAL_P12") or "").strip(),
        "password": os.getenv("ESIGN_SEAL_P12_PASS") or "",
        "reason": (os.getenv("ESIGN_SEAL_REASON")
                   or "Executed through Kavachio — all parties have signed."),
        "location": (os.getenv("ESIGN_SEAL_LOCATION") or "").strip(),
        "tsa": (os.getenv("ESIGN_SEAL_TSA_URL") or "").strip(),
        # Long-term validation: embed the CA's revocation data so the signature
        # can still be checked offline years from now. Separate from the
        # timestamp and OFF by default, because it makes signing reach out to
        # the CA's OCSP/CRL endpoints — which a self-signed development
        # certificate does not have, and which turn a CA outage into a contract
        # that completes unsealed.
        "ltv": (os.getenv("ESIGN_SEAL_LTV") or "").strip().lower()
               in ("1", "true", "yes", "on"),
    }


def is_configured() -> bool:
    """Whether a certificate is actually available to sign with."""
    p12 = _cfg()["p12"]
    return bool(p12) and os.path.isfile(p12)


def describe() -> dict[str, Any]:
    """What was used, for the audit event. Never includes the passphrase."""
    c = _cfg()
    return {"field": FIELD_NAME, "certificate": os.path.basename(c["p12"]),
            "timestamped": bool(c["tsa"]), "ltv": bool(c["ltv"] and c["tsa"])}


def _normalise(pdf_bytes: bytes) -> bytes:
    """Re-save the PDF so a strict reader will accept it.

    PyMuPDF's `garbage=2`+ modes renumber objects and, doing so, write the head
    of the cross-reference table with generation 65536 where the spec says
    65535. Every ordinary viewer shrugs at that; pyHanko, correctly, refuses to
    sign a file it cannot parse strictly:

        PdfStrictReadError: Illegal generation 65536 for object ID 0

    `esign_pdf` saves with garbage=3 because the working copy is rewritten on
    every signature and object compaction keeps it small, so the fix belongs
    here rather than there — re-saved once, at the end, with the compaction
    turned down to a level that emits a valid table. Still deflated, so the
    delivered file does not balloon.

    This has to happen BEFORE the signature: any rewrite after it would be
    precisely the tampering the seal exists to detect.
    """
    import fitz
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        return doc.tobytes(deflate=True, garbage=1)


def _signer(c: dict[str, str]):
    from pyhanko.sign import signers
    return signers.SimpleSigner.load_pkcs12(
        pfx_file=c["p12"],
        passphrase=c["password"].encode() if c["password"] else None)


def seal(pdf_bytes: bytes, *, title: str = "", envelope_id: int | None = None
         ) -> bytes | None:
    """Apply the platform seal. Returns the sealed PDF, or None if it did not
    happen — no certificate configured, or the attempt failed.

    None is a normal outcome, not an error to propagate. The caller keeps the
    unsealed document and the contract completes regardless; the whole point is
    that this can never be the reason a signed contract fails to be delivered.
    """
    global _warned
    c = _cfg()
    if not is_configured():
        if not _warned:
            _warned = True
            log.warning(
                "[esign] no signing certificate configured (ESIGN_SEAL_P12) — "
                "completed contracts are being delivered UNSEALED. They carry "
                "the signatures and the audit trail, but a reader cannot prove "
                "the file has not been edited since.")
        return None

    try:
        from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
        from pyhanko.sign import fields, signers
        from pyhanko.sign.timestamps import HTTPTimeStamper
        from pyhanko_certvalidator import ValidationContext

        stamper = HTTPTimeStamper(url=c["tsa"]) if c["tsa"] else None
        # LTV needs something to timestamp against; without one there is no
        # point embedding revocation data that nothing can be checked against.
        ltv = bool(c["ltv"] and stamper)
        if c["ltv"] and not stamper:
            log.warning("[esign] ESIGN_SEAL_LTV needs ESIGN_SEAL_TSA_URL too — "
                        "sealing without long-term validation data")
        meta = signers.PdfSignatureMetadata(
            field_name=FIELD_NAME,
            reason=c["reason"],
            location=c["location"] or None,
            # PAdES is the ISO 32000/ETSI profile European and Indian readers
            # expect; it is also what makes a long-term timestamp meaningful.
            subfilter=fields.SigSeedSubFilter.PADES,
            # A CERTIFICATION signature, not an approval one: it declares the
            # document finished. NO_CHANGES means any later edit — including
            # filling a form field or adding an annotation — breaks it. That is
            # correct here precisely because nothing should ever touch this
            # file again; it is applied after the last signature is stamped.
            certify=True,
            docmdp_permissions=fields.MDPPerm.NO_CHANGES,
            # Only with ESIGN_SEAL_LTV, and only alongside a timestamp: pyHanko
            # refuses to embed validation info without a validation context, so
            # asking for one without the other used to fail the seal outright
            # and deliver the contract unsealed.
            embed_validation_info=ltv,
            validation_context=(ValidationContext(allow_fetching=True) if ltv else None),
        )
        # Invisible: no widget on the page. The visible signature blocks were
        # already stamped where the anchors put them, and a second, differently
        # placed box drawn by the signing library on top of them would be a
        # confusing duplicate of something the parties already saw.
        signer = signers.PdfSigner(meta, signer=_signer(c), timestamper=stamper)
        out = signer.sign_pdf(IncrementalPdfFileWriter(BytesIO(_normalise(pdf_bytes))))
        data = out.getvalue() if hasattr(out, "getvalue") else out
        log.info("[esign] sealed envelope %s (%s)", envelope_id, title)
        return data
    except Exception:
        # Logged in full, swallowed on purpose. See the docstring: a failed
        # seal must not lose a signature that genuinely happened.
        log.exception("[esign] could not seal envelope %s — delivering unsealed",
                      envelope_id)
        return None
