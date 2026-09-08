"""Check the seal on a signed contract.

    python scripts/verify_signed_pdf.py certs/sample-signed-sealed.pdf

Exists because the readers on this machine mostly cannot do it. Adobe Acrobat
shows signature status properly; evince ignores signatures entirely, and a
document that merely OPENS in it tells you nothing at all about whether it has
been altered. So there is a real trap here — "it looks fine in the PDF viewer"
is not a check — and this is the answer to it.

Trusts certs/kavachio-dev-root.crt by default, plus the system trust store, so
a contract sealed with either the development certificate or a bought one
verifies with no arguments.

Exit code is 0 only when the signature is intact AND trusted, so it can be used
in a script.
"""

from __future__ import annotations

import argparse
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CERTS = os.path.join(os.path.dirname(HERE), "certs")
DEV_ROOT = os.path.join(CERTS, "kavachio-dev-root.crt")


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify the seal on a signed PDF.")
    ap.add_argument("pdf")
    ap.add_argument("--root", action="append", default=[],
                    help="extra trusted root (PEM/DER). Repeatable.")
    args = ap.parse_args()

    from pyhanko.keys import load_cert_from_pemder
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation import validate_pdf_signature
    from pyhanko_certvalidator import ValidationContext

    roots = []
    for path in ([DEV_ROOT] if os.path.isfile(DEV_ROOT) else []) + args.root:
        try:
            roots.append(load_cert_from_pemder(path))
        except Exception as e:
            print(f"could not read root {path}: {e}", file=sys.stderr)

    with open(args.pdf, "rb") as f:
        data = f.read()

    sigs = PdfFileReader(io.BytesIO(data)).embedded_signatures
    if not sigs:
        print("NOT SEALED — this file carries no digital signature at all.")
        print("Anyone can edit it and nothing will say so. If it came out of")
        print("Kavachio, ESIGN_SEAL_P12 was not configured when it completed.")
        return 2

    vc = ValidationContext(trust_roots=roots, allow_fetching=False,
                           revocation_mode="soft-fail")
    ok = True
    for sig in sigs:
        st = validate_pdf_signature(sig, vc)
        print(f"field      : {sig.field_name}")
        print(f"signed by  : {st.signing_cert.subject.human_friendly}")
        print(f"covers     : {st.coverage.name}")
        print(f"unmodified : {st.intact}")
        print(f"trusted    : {st.trusted}")
        print(f"summary    : {st.summary()}")
        if not st.intact:
            print("\n  ** THIS FILE HAS BEEN ALTERED SINCE IT WAS SIGNED. **")
        elif not st.trusted:
            # Worth separating: the document is fine, the certificate is simply
            # not one this machine has been told to believe. That is the normal
            # state for the development certificate until its root is installed.
            print("\n  Intact, but the signing certificate is not trusted here.")
            print(f"  Install the root, or pass --root {DEV_ROOT}")
        ok = ok and st.intact and st.trusted
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
