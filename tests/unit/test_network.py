import datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from airbound.network import certificate_owner


def self_signed(**names: str) -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(getattr(NameOID, k), v) for k, v in names.items()])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.DER)


def test_owner_names_router_and_vendor():
    assert certificate_owner(self_signed(COMMON_NAME="UniFi", ORGANIZATION_NAME="Ubiquiti Inc.")) == "UniFi (Ubiquiti Inc.)"


def test_owner_with_only_common_name():
    assert certificate_owner(self_signed(COMMON_NAME="amp.local")) == "amp.local"


def test_owner_of_garbage_is_empty():
    assert certificate_owner(b"not a certificate") == ""
