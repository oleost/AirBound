"""DNS for the relay's domain: the domain itself and *.domain point at this server."""

from __future__ import annotations

from aiohttp import ClientSession

from .config import Config

API = "https://api.cloudflare.com/client/v4"


async def ensure_records(session: ClientSession, config: Config) -> list[str]:
    headers = {"Authorization": f"Bearer {config.cloudflare_token}"}
    async with session.get(f"{API}/zones", params={"name": config.domain}, headers=headers) as resp:
        zones = (await resp.json())["result"]
    if not zones:
        raise SystemExit(f"The Cloudflare token can't see {config.domain}")
    zone = zones[0]["id"]
    done = []
    for name in (config.domain, f"*.{config.domain}"):
        async with session.get(f"{API}/zones/{zone}/dns_records", params={"type": "A", "name": name}, headers=headers) as resp:
            existing = (await resp.json())["result"]
        record = {"type": "A", "name": name, "content": config.public_ip, "ttl": 300, "proxied": False, "comment": "AirBound hosted relay"}
        if existing:
            url, method = f"{API}/zones/{zone}/dns_records/{existing[0]['id']}", "PUT"
        else:
            url, method = f"{API}/zones/{zone}/dns_records", "POST"
        async with session.request(method, url, json=record, headers=headers) as resp:
            body = await resp.json()
            if not body.get("success"):
                raise SystemExit(f"Cloudflare refused {name}: {body.get('errors')}")
        done.append(f"{name} -> {config.public_ip}")
    return done
