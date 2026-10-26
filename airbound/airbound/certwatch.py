"""Certificate Transparency watch: has anyone been issued a certificate for the hostname?

Every public certificate is logged in Certificate Transparency. AirBound only ever
gets a wildcard certificate for the domain, and a CAA record forbids certificates
for the exact hostname, so a logged certificate naming the exact hostname means
something is wrong.

On the hosted relay's domain the add-on gets a certificate for the exact
hostname itself, so there a certificate is unexpected when it covers the
hostname (directly or as a wildcard) and has a public key this Home Assistant
never made. Precertificates and final certificates share the key, so comparing
keys works for both. Only certificates issued after the Home Assistant was
linked count: its random name didn't exist before, and a wildcard issued
earlier (say, by the DNS provider's automatic certificates) is a known
limitation of any shared domain.

The logs are searched for the whole domain, never the hostname itself, so the
hidden hostname isn't revealed to a third party. Results are filtered here.
Cert Spotter (SSLMate) is asked first; crt.sh, which is often overloaded, is the
fallback.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout

_LOGGER = logging.getLogger(__name__)

CERTSPOTTER = "https://api.certspotter.com/v1/issuances"
CRT_SH = "https://crt.sh/"


CADDY_CERTIFICATES = Path("/data/caddy/certificates")


def unexpected_certificates(
    certificates: list[dict], fqdn: str, own_keys: set[str] | None = None, since: str = ""
) -> list[dict]:
    """Logged certificates for the hostname that AirBound didn't order.

    Each certificate is {"id", "names", "issuer", "not_before", "pubkey"}. Without
    own_keys (our own domain), any certificate naming the exact hostname counts;
    wildcards are AirBound's own. With own_keys (hosted relay), any certificate
    covering the hostname counts unless it has one of our keys.
    """
    fqdn = fqdn.lower()
    wildcard = "*." + fqdn.split(".", 1)[1] if "." in fqdn else ""
    found = {}
    for cert in certificates:
        names = {n.strip().lower() for n in cert.get("names", [])}
        if own_keys is None:
            if fqdn in names:
                found[cert["id"]] = {**cert, "url": f"{CRT_SH}?q={fqdn}"}
        elif (
            (fqdn in names or wildcard in names)
            and cert.get("pubkey")
            and cert["pubkey"] not in own_keys
            and cert.get("not_before", "") >= since  # ISO 8601 UTC strings sort by time
        ):
            found[cert["id"]] = {**cert, "url": f"{CRT_SH}?q={fqdn}"}
    return list(found.values())


def own_public_keys(fqdn: str, root: Path = CADDY_CERTIFICATES) -> set[str]:
    """SHA-256 of the public keys in the certificates Caddy holds for the hostname."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    keys = set()
    for path in root.glob(f"*/{fqdn}/{fqdn}.crt"):
        try:
            cert = x509.load_pem_x509_certificate(path.read_bytes())
        except ValueError:
            continue
        der = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        keys.add(hashlib.sha256(der).hexdigest())
    return keys


def from_certspotter(entries: list[dict]) -> list[dict]:
    return [
        {
            "id": f"certspotter:{e.get('id')}",
            "names": e.get("dns_names") or [],
            "issuer": (e.get("issuer") or {}).get("friendly_name") or (e.get("issuer") or {}).get("name", ""),
            "not_before": e.get("not_before", ""),
            "pubkey": e.get("pubkey_sha256", ""),
        }
        for e in entries
    ]


def from_crtsh(entries: list[dict]) -> list[dict]:
    return [
        {
            "id": f"crtsh:{e.get('id')}",
            "names": str(e.get("name_value", "")).splitlines(),
            "issuer": e.get("issuer_name", ""),
            "not_before": e.get("not_before", ""),
        }
        for e in entries
    ]


class CertWatch:
    def __init__(self) -> None:
        self.unexpected: list[dict] = []
        self.checked = False  # False until one lookup has succeeded
        self.fqdn = ""  # the hostname the results are for

    async def _get_json(self, session: ClientSession, url: str, params: list[tuple[str, str]]):
        try:
            async with session.get(url, params=params, timeout=ClientTimeout(total=60)) as resp:
                if resp.status != 200:
                    _LOGGER.info("Certificate Transparency lookup at %s returned %s", url, resp.status)
                    return None
                return await resp.json(content_type=None)
        except (ClientError, TimeoutError, ValueError) as err:
            _LOGGER.info("Certificate Transparency lookup at %s failed: %s", url, err)
            return None

    def results_for(self, fqdn: str) -> tuple[bool, list[dict]]:
        """(checked, unexpected) for this hostname; results for an earlier address don't count."""
        if self.fqdn != fqdn:
            return False, []
        return self.checked, self.unexpected

    async def scan(
        self, session: ClientSession, domain: str, fqdn: str, own_keys: set[str] | None = None, since: str = ""
    ) -> list[dict] | None:
        """Returns the unexpected certificates, or None if no log search could be reached."""
        entries = await self._get_json(
            session,
            CERTSPOTTER,
            [("domain", domain), ("include_subdomains", "true"), ("expand", "dns_names"), ("expand", "issuer")],
        )
        certificates = from_certspotter(entries) if isinstance(entries, list) else None
        if certificates is None:
            entries = await self._get_json(
                session, CRT_SH, [("q", f"%.{domain}"), ("output", "json"), ("exclude", "expired")]
            )
            certificates = from_crtsh(entries) if isinstance(entries, list) else None
        if certificates is None:
            return None
        self.unexpected = unexpected_certificates(certificates, fqdn, own_keys, since)
        self.checked = True
        self.fqdn = fqdn
        _LOGGER.info("Certificate Transparency: %d certificates for the domain, %d unexpected", len(certificates), len(self.unexpected))
        return self.unexpected
