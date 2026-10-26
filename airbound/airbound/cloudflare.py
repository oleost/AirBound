"""Cloudflare API: token checks, zone lookup and the DNS record for the hostname."""

from __future__ import annotations

from typing import Any

from aiohttp import ClientError, ClientSession

API = "https://api.cloudflare.com/client/v4"


class CloudflareError(Exception):
    """An error with a message that can be shown to the user as-is."""


class Cloudflare:
    def __init__(self, session: ClientSession, token: str) -> None:
        self._session = session
        self._token = token.strip()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            async with self._session.request(
                method, f"{API}{path}", headers={"Authorization": f"Bearer {self._token}"}, **kwargs
            ) as resp:
                body = await resp.json(content_type=None)
                status = resp.status
        except (ClientError, TimeoutError) as err:
            raise CloudflareError("AirBound couldn't reach Cloudflare. Check that Home Assistant has internet access.") from err
        if status in (400, 401) or (status == 403 and path.startswith("/user/tokens")):
            raise CloudflareError("Cloudflare didn't accept this token. Check that you copied all of it.")
        if status == 403:
            raise CloudflareError(
                "The token works, but it isn't allowed to change DNS for this domain. "
                "Create it again with the permissions shown above."
            )
        if not body.get("success"):
            errors = "; ".join(e.get("message", "") for e in body.get("errors", []))
            raise CloudflareError(f"Cloudflare returned an error: {errors or status}")
        return body["result"]

    async def verify(self) -> None:
        result = await self._request("GET", "/user/tokens/verify")
        if result.get("status") != "active":
            raise CloudflareError("This token is disabled or expired. Create a new one.")

    async def zones(self) -> list[dict[str, str]]:
        result = await self._request("GET", "/zones", params={"per_page": "50", "status": "active"})
        zones = [{"id": z["id"], "name": z["name"]} for z in result]
        if not zones:
            raise CloudflareError(
                "The token works, but it can't see any domains. "
                "Under \"Zone Resources\", include the domain you want to use."
            )
        return sorted(zones, key=lambda z: z["name"])

    async def lock_certificates(self, zone_id: str, name: str) -> None:
        """Forbid every CA from issuing a certificate for exactly this hostname.

        AirBound's own certificate is a wildcard for the domain, which CAs check
        against CAA on the domain itself, so this record doesn't affect it or any
        other name in the zone. It stops anyone who controls where traffic for
        the hostname goes (such as a relay operator) from getting a certificate
        for it through an HTTP-01 or TLS-ALPN-01 check.
        """
        existing = await self._request("GET", f"/zones/{zone_id}/dns_records", params={"type": "CAA", "name": name})
        for record in existing:
            data = record.get("data") or {}
            if data.get("tag") == "issue" and data.get("value") == ";":
                return
        await self._request(
            "POST",
            f"/zones/{zone_id}/dns_records",
            json={
                "type": "CAA",
                "name": name,
                "data": {"flags": 0, "tag": "issue", "value": ";"},
                "ttl": 3600,
                "comment": "Managed by AirBound: no certificates for this exact name",
            },
        )

    async def certificates_locked(self, zone_id: str, name: str) -> bool:
        records = await self._request("GET", f"/zones/{zone_id}/dns_records", params={"type": "CAA", "name": name})
        return any((r.get("data") or {}).get("value") == ";" for r in records)

    async def upsert_a_record(self, zone_id: str, name: str, ip: str) -> None:
        """Point name at ip, DNS only (grey cloud) so Cloudflare never sees the traffic."""
        existing = await self._request("GET", f"/zones/{zone_id}/dns_records", params={"type": "A", "name": name})
        record = {"type": "A", "name": name, "content": ip, "ttl": 300, "proxied": False, "comment": "Managed by AirBound"}
        if existing:
            await self._request("PUT", f"/zones/{zone_id}/dns_records/{existing[0]['id']}", json=record)
        else:
            await self._request("POST", f"/zones/{zone_id}/dns_records", json=record)
