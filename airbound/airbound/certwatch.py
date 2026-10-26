"""Certificate Transparency watch: has anyone been issued a certificate for the hostname?

Every public certificate is logged in Certificate Transparency. AirBound only ever
gets a wildcard certificate for the domain, and a CAA record forbids certificates
for the exact hostname, so a logged certificate naming the exact hostname means
something is wrong.

The logs are searched for the whole domain, never the hostname itself, so the
hidden hostname isn't revealed to a third party. Results are filtered here.
Cert Spotter (SSLMate) is asked first; crt.sh, which is often overloaded, is the
fallback.
"""

from __future__ import annotations

import logging

from aiohttp import ClientError, ClientSession, ClientTimeout

_LOGGER = logging.getLogger(__name__)

CERTSPOTTER = "https://api.certspotter.com/v1/issuances"
CRT_SH = "https://crt.sh/"


def unexpected_certificates(certificates: list[dict], fqdn: str) -> list[dict]:
    """Logged certificates that name the exact hostname (wildcards excluded).

    Each certificate is {"id", "names", "issuer", "not_before"}.
    """
    found = {}
    for cert in certificates:
        names = {n.strip().lower() for n in cert.get("names", [])}
        if fqdn.lower() in names:
            found[cert["id"]] = {**cert, "url": f"{CRT_SH}?q={fqdn}"}
    return list(found.values())


def from_certspotter(entries: list[dict]) -> list[dict]:
    return [
        {
            "id": f"certspotter:{e.get('id')}",
            "names": e.get("dns_names") or [],
            "issuer": (e.get("issuer") or {}).get("friendly_name") or (e.get("issuer") or {}).get("name", ""),
            "not_before": e.get("not_before", ""),
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

    async def scan(self, session: ClientSession, domain: str, fqdn: str) -> list[dict] | None:
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
        self.unexpected = unexpected_certificates(certificates, fqdn)
        self.checked = True
        _LOGGER.info("Certificate Transparency: %d certificates for the domain, %d unexpected", len(certificates), len(self.unexpected))
        return self.unexpected
