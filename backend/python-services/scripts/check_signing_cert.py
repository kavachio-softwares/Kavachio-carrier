"""Check a signing certificate BEFORE putting it into .env.

    python scripts/check_signing_cert.py /path/to/client.pfx
    python scripts/check_signing_cert.py            # checks what .env points at

Answers one question: will this certificate seal contracts correctly, and for
how long. It reads the bundle, reports what a PDF reader will make of it, then
actually seals a throwaway document and verifies the result — because a
certificate that parses is not the same as a certificate that signs.

Read-only. It never writes to the bundle and never touches the database.
"""

from __future__ import annotations

import datetime as dt
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DOC_SIGNING_OID = "1.3.6.1.5.5.7.3.36"
EMAIL_OID = "1.3.6.1.5.5.7.3.4"

problems: list[str] = []
warnings: list[str] = []


def bad(msg: str) -> None:
    problems.append(msg)
    print(f"  \033[31mPROBLEM\033[0m  {msg}")


def warn(msg: str) -> None:
    warnings.append(msg)
    print(f"  \033[33mnote\033[0m     {msg}")


def ok(msg: str) -> None:
    print(f"  \033[32mok\033[0m       {msg}")


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), ".env"))

    path = sys.argv[1] if len(sys.argv) > 1 else os.getenv("ESIGN_SEAL_P12", "")
    password = os.getenv("ESIGN_SEAL_P12_PASS", "")
    if len(sys.argv) > 2:
        password = sys.argv[2]

    if not path:
        print("no certificate given, and ESIGN_SEAL_P12 is not set.")
        return 2
    print(f"\ncertificate: {path}\n")
    if not os.path.isfile(path):
        bad("file does not exist")
        return 2

    mode = oct(os.stat(path).st_mode & 0o777)
    if os.stat(path).st_mode & 0o077:
        warn(f"readable by other users (mode {mode}) — it holds a PRIVATE KEY. "
             f"chmod 600 it.")
    else:
        ok(f"permissions {mode}")

    # ── load ────────────────────────────────────────────────────────────────
    from cryptography.hazmat.primitives.serialization import pkcs12
    try:
        key, cert, chain = pkcs12.load_key_and_certificates(
            open(path, "rb").read(), password.encode() if password else None)
    except Exception as e:
        bad(f"could not open the bundle: {e}")
        print("\n  If it asks for a password, set ESIGN_SEAL_P12_PASS or pass it "
              "as the second argument.")
        return 2

    if key is None:
        bad("no PRIVATE KEY in this bundle — it is a certificate only, and "
            "cannot sign anything. Ask the issuer for the .pfx/.p12 that "
            "includes the key.")
    else:
        ok(f"private key present ({key.key_size}-bit)")
        if key.key_size < 2048:
            bad(f"{key.key_size}-bit key is below the 2048-bit minimum readers accept")

    if cert is None:
        bad("no certificate in the bundle")
        return 2

    print()
    print(f"  subject  {cert.subject.rfc4514_string()}")
    print(f"  issuer   {cert.issuer.rfc4514_string()}")
    print(f"  serial   {cert.serial_number:x}")
    print(f"  valid    {cert.not_valid_before_utc:%d %b %Y} "
          f"to {cert.not_valid_after_utc:%d %b %Y}")
    print()

    # ── validity window ─────────────────────────────────────────────────────
    now = dt.datetime.now(dt.timezone.utc)
    if now < cert.not_valid_before_utc:
        bad("not valid yet")
    elif now > cert.not_valid_after_utc:
        bad("EXPIRED — sealing will fail or produce signatures nobody accepts")
    else:
        days = (cert.not_valid_after_utc - now).days
        (ok if days > 60 else warn)(f"valid for another {days} days")
        if days <= 60:
            warn("plan the renewal now. A contract sealed on the last day is "
                 "fine, but the day after that nothing seals at all.")

    # ── what a reader checks ────────────────────────────────────────────────
    from cryptography import x509
    try:
        ku = cert.extensions.get_extension_for_class(x509.KeyUsage).value
        if not ku.digital_signature:
            bad("Key Usage does not allow digital signature — readers will "
                "reject every seal made with it")
        else:
            ok("Key Usage allows digital signature"
               + (" and non-repudiation" if ku.content_commitment else ""))
        if not ku.content_commitment:
            warn("no non-repudiation (contentCommitment) bit. Usable, but some "
                 "readers treat it as weaker for signatures meant to bind.")
    except x509.ExtensionNotFound:
        warn("no Key Usage extension — permissive, but unusual for a real DSC")

    try:
        eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        oids = {e.dotted_string for e in eku}
        if DOC_SIGNING_OID in oids or EMAIL_OID in oids:
            ok("Extended Key Usage permits document signing")
        elif "1.3.6.1.5.5.7.3.2" in oids or "1.3.6.1.5.5.7.3.1" in oids:
            bad("this looks like a TLS/web-server certificate, not a document "
                "signing one. It will not be accepted for signing PDFs.")
        else:
            warn(f"unrecognised Extended Key Usage: {', '.join(sorted(oids))}")
    except x509.ExtensionNotFound:
        ok("no Extended Key Usage restriction (usable for signing)")

    try:
        bc = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
        if bc.ca:
            warn("this is a CA certificate, not an end-entity one. Signing with "
                 "a CA key works but is bad practice — ask for a leaf.")
    except x509.ExtensionNotFound:
        pass

    if cert.subject == cert.issuer:
        warn("SELF-SIGNED — fine for development, but no counterparty's reader "
             "will trust it until they install it by hand.")
    elif chain:
        ok(f"issuing chain bundled ({len(chain)} certificate(s))")
    else:
        warn("no intermediate certificates bundled. If the issuer is a public "
             "CA with an intermediate, readers may fail to build the chain. "
             "Ask for the full chain in the .p12.")

    # revocation endpoints matter for long-term validation
    try:
        cert.extensions.get_extension_for_class(x509.CRLDistributionPoints)
        ok("CRL distribution point present (ESIGN_SEAL_LTV is usable)")
    except x509.ExtensionNotFound:
        warn("no CRL distribution point — leave ESIGN_SEAL_LTV off")

    # ── the real test: actually seal something ──────────────────────────────
    print()
    os.environ["ESIGN_SEAL_P12"] = path
    os.environ["ESIGN_SEAL_P12_PASS"] = password
    import fitz
    import esign_seal
    doc = fitz.open()
    doc.new_page().insert_text((72, 100), "preflight")
    sealed = esign_seal.seal(doc.tobytes(), title="preflight", envelope_id=0)
    if not sealed:
        bad("TEST SEAL FAILED — see the traceback above. The certificate loads "
            "but cannot actually sign.")
    else:
        from pyhanko.pdf_utils.reader import PdfFileReader
        sig = PdfFileReader(io.BytesIO(sealed)).embedded_signatures[0]
        cov = sig.evaluate_signature_coverage()
        ok(f"test seal produced and covers {cov.name}")

    # ── verdict ─────────────────────────────────────────────────────────────
    print()
    if problems:
        print(f"  \033[31m{len(problems)} problem(s) — do not deploy this yet.\033[0m")
        return 1
    if warnings:
        print(f"  \033[33mUsable, with {len(warnings)} thing(s) worth reading above.\033[0m")
        return 0
    print("  \033[32mGood. Point ESIGN_SEAL_P12 at it and restart.\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
