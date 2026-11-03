"""Make a development signing certificate for esign_seal.py.

    python scripts/make_dev_signing_cert.py

Produces a two-level chain in `certs/`:

    kavachio-dev-root.crt      the root. INSTALL THIS ONCE in your PDF reader.
    kavachio-dev-signer.p12    key + chain, what ESIGN_SEAL_P12 points at.

WHY A ROOT AND A LEAF, RATHER THAN ONE SELF-SIGNED CERTIFICATE
--------------------------------------------------------------
A lone self-signed certificate has to be trusted individually, and re-trusted
every time it is regenerated. With a root you trust once, every certificate
issued under it validates — which is how the real thing works, so testing
against this shape tells you something about production rather than about a
shortcut. It also lets you re-issue the signer (expiry, key rotation, a second
environment) without touching anybody's reader again.

WHAT THIS IS NOT
----------------
It is NOT a legally recognised DSC. A real one comes from a CA licensed by the
CCA in India (eMudhra, Capricorn, Vsign…) or a public CA elsewhere, and what you
pay for is precisely the part this script cannot fake: that the CA checked who
you are, and that its root is already in everyone's reader. The cryptography
below is identical; the trust is not.

So this is exactly right for proving the seal works, showing it to colleagues,
and building the flow against it — and wrong for anything a counterparty is
expected to rely on. Swapping in the bought certificate is one .env change.
"""

from __future__ import annotations

import datetime as dt
import os
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "certs")

ORG = os.getenv("DEV_CERT_ORG", "Kavachio")
COUNTRY = os.getenv("DEV_CERT_COUNTRY", "IN")
PASSWORD = os.getenv("DEV_CERT_PASS", "kavachio-dev")

ROOT_CN = "Kavachio Development Root CA"
LEAF_CN = "Kavachio Document Signer (Development)"

# Adobe accepts either of these on a document-signing certificate. Both are set:
# the modern one (id-kp-documentSigning, RFC 9336) and the one older readers
# have always looked for.
DOCUMENT_SIGNING = x509.ObjectIdentifier("1.3.6.1.5.5.7.3.36")


def _name(cn: str) -> x509.Name:
    return x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, COUNTRY),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, ORG),
        x509.NameAttribute(NameOID.COMMON_NAME, cn),
    ])


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)

    # ── the root ────────────────────────────────────────────────────────────
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    root = (
        x509.CertificateBuilder()
        .subject_name(_name(ROOT_CN)).issuer_name(_name(ROOT_CN))
        .public_key(root_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=3650))
        # path_length=0: this root may sign end-entity certificates and nothing
        # else. A development root that can mint further CAs is a development
        # root that can impersonate anybody, on any machine that trusts it.
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=False, content_commitment=False,
            key_encipherment=False, data_encipherment=False, key_agreement=False,
            key_cert_sign=True, crl_sign=True,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()),
                       critical=False)
        .sign(root_key, hashes.SHA256())
    )

    # ── the signer ──────────────────────────────────────────────────────────
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(_name(LEAF_CN)).issuer_name(root.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=730))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        # digital_signature + content_commitment is the pair a reader looks for
        # on a signature it is being asked to treat as non-repudiable.
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=True,
            key_encipherment=False, data_encipherment=False, key_agreement=False,
            key_cert_sign=False, crl_sign=False,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage(
            [DOCUMENT_SIGNING, ExtendedKeyUsageOID.EMAIL_PROTECTION]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()),
                       critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
            root_key.public_key()), critical=False)
        .sign(root_key, hashes.SHA256())
    )

    root_path = os.path.join(OUT, "kavachio-dev-root.crt")
    p12_path = os.path.join(OUT, "kavachio-dev-signer.p12")

    with open(root_path, "wb") as f:
        f.write(root.public_bytes(serialization.Encoding.PEM))

    # The chain travels inside the bundle, so the signed PDF carries the root
    # with it and a reader that already trusts the root can build the path
    # without fetching anything.
    p12 = pkcs12.serialize_key_and_certificates(
        name=b"kavachio-dev-signer", key=leaf_key, cert=leaf, cas=[root],
        encryption_algorithm=serialization.BestAvailableEncryption(PASSWORD.encode()))
    with open(p12_path, "wb") as f:
        f.write(p12)
    os.chmod(p12_path, 0o600)      # it holds a private key

    print(f"root  : {root_path}")
    print(f"signer: {p12_path}  (passphrase: {PASSWORD})")
    print(f"        valid until {leaf.not_valid_after_utc:%d %b %Y}")
    print()
    print("Point the app at it:")
    print(f"    ESIGN_SEAL_P12={p12_path}")
    print(f"    ESIGN_SEAL_P12_PASS={PASSWORD}")
    print()
    print("Then install the ROOT in your PDF reader once, or every signature")
    print("will read 'validity unknown' — which is the reader being correct.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
