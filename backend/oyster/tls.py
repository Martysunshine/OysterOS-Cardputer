"""Private development CA for the HTTPS listener (ADR-003).

`oyster make-dev-ca --ip <LAN-IP>` writes into data/dev-ca/:
  ca.key, ca.crt          - the CA (reused on later runs unless new_ca=True, so provisioned devices
                            keep trusting it)
  server.key, server.crt  - server certificate with CN = the LAN IP and SAN = IPAddress(ip) + DNSName(ip)
                            (ESP32 mbedTLS 2.x matches the host string it connected to)
Private keys are written unencrypted (development material, not tamper-proof storage) with mode 0600, or on
Windows with an ACL reduced to the current user (oyster.fileperms). DevCaResult.warnings lists any key
whose owner-only protection could not be applied or verified; callers must not claim protection then.
Keys are never printed. Only paths and certificate fingerprints are reported.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from oyster.fileperms import write_private_file

CA_DAYS = 3650
SERVER_DAYS = 825
KEY_BITS = 2048  # RSA-2048: verified by every mbedTLS build used on ESP32


@dataclass
class DevCaResult:
    ca_cert: Path
    ca_key: Path
    server_cert: Path
    server_key: Path
    ca_created: bool
    ca_sha256: str
    server_sha256: str
    ip: str
    warnings: list[str] = field(default_factory=list)  # keys whose owner-only protection is not confirmed


def _write_private(path: Path, data: bytes, warnings: list[str]) -> None:
    warning = write_private_file(path, data)
    if warning:
        warnings.append(warning)


def _key_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def fingerprint(cert: x509.Certificate) -> str:
    return cert.fingerprint(hashes.SHA256()).hex(":").upper()


def _new_ca(now: dt.datetime) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_BITS)
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "Oyster Pocket Dev CA"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Oyster Pocket (development)"),
        ]
    )
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(ski, critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _server_cert(ip: str, ca_key, ca_cert: x509.Certificate, now: dt.datetime):
    key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_BITS)
    ca_ski = ca_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ip)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=SERVER_DAYS))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(ip)), x509.DNSName(ip)]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def make_dev_ca(ip: str, out_dir: str | os.PathLike, *, new_ca: bool = False) -> DevCaResult:
    ip = str(ipaddress.ip_address(ip.strip()))  # ValueError for anything that is not an IP literal
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ca_crt, ca_key_p = out / "ca.crt", out / "ca.key"
    srv_crt, srv_key_p = out / "server.crt", out / "server.key"
    now = dt.datetime.now(dt.UTC)
    created = False
    warnings: list[str] = []
    if new_ca or not (ca_crt.is_file() and ca_key_p.is_file()):
        ca_key, ca_cert = _new_ca(now)
        _write_private(ca_key_p, _key_pem(ca_key), warnings)
        ca_crt.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
        created = True
    else:
        ca_key = serialization.load_pem_private_key(ca_key_p.read_bytes(), password=None)
        ca_cert = x509.load_pem_x509_certificate(ca_crt.read_bytes())
    key, cert = _server_cert(ip, ca_key, ca_cert, now)
    _write_private(srv_key_p, _key_pem(key), warnings)
    srv_crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return DevCaResult(
        ca_crt, ca_key_p, srv_crt, srv_key_p, created, fingerprint(ca_cert), fingerprint(cert), ip, warnings
    )


def server_cert_ip(cert_path: str | os.PathLike) -> str | None:
    """First IP address named in the server certificate SAN (the default backend host for provisioning)."""
    p = Path(cert_path)
    if not p.is_file():
        return None
    cert = x509.load_pem_x509_certificate(p.read_bytes())
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return None
    ips = san.get_values_for_type(x509.IPAddress)
    return str(ips[0]) if ips else None
