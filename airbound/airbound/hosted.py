"""Hosted mode: Home Assistant connects out to AirBound's shared relay.

Linking works like signing in a TV: AirBound asks the relay's website for a link
request, shows a short code and a link, the person logs in with Patreon there,
and AirBound picks up its own key and address. The relay routes connections by
name without decrypting them; the certificate is made here, at home.

The relay's certificate authority is pinned: the CA shipped with the add-on is
the only one frpc trusts, whatever the website sends.
"""

from __future__ import annotations

import os
from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout

SERVICE = os.environ.get("AIRBOUND_HOSTED_SERVICE", "https://airbound.dev")
RELAY_HOST = os.environ.get("AIRBOUND_HOSTED_RELAY", "relay.airbound.dev")
RELAY_PORT = 7000
SERVER_NAME = "airbound-hosted-relay"  # the name in the relay's certificate
AUTH_TOKEN = "airbound-hosted-relay"  # not a secret: the device key is what counts
CA_FILE = Path(__file__).parent / "hosted_relay_ca.pem"
TIMEOUT = ClientTimeout(total=15)


class HostedError(Exception):
    pass


async def _call(session: ClientSession, method: str, path: str, token: str = "", body: dict | None = None) -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with session.request(method, f"{SERVICE}{path}", json=body, headers=headers, timeout=TIMEOUT) as resp:
            data = await resp.json(content_type=None)
            return resp.status, data if isinstance(data, dict) else {}
    except (ClientError, TimeoutError, ValueError) as err:
        raise HostedError("AirBound couldn't reach the hosted relay's website. Check that Home Assistant has internet access.") from err


async def start_link(session: ClientSession, install_id: str) -> dict:
    """{claim, poll, code, link_url}: a link request waiting for the Patreon login."""
    status, data = await _call(session, "POST", "/api/v1/claims", body={"install_id": install_id})
    if status != 200:
        raise HostedError(data.get("error") or f"The hosted relay answered {status}. Try again in a moment.")
    return data


async def poll_link(session: ClientSession, claim: str, poll: str) -> dict:
    """{state: pending | needs_membership | approved | delivered | expired, ...}"""
    status, data = await _call(session, "GET", f"/api/v1/claims/{claim}", token=poll)
    if status == 404:
        return {"state": "expired"}
    if status != 200:
        raise HostedError(f"The hosted relay answered {status}. Try again in a moment.")
    return data


async def account(session: ClientSession, key: str) -> dict:
    """{state: active | grace | expired | unlinked, fqdn, grace_until, join_url, manage_url}"""
    status, data = await _call(session, "GET", "/api/v1/device", token=key)
    if status == 404:
        return {"state": "unlinked"}
    if status != 200:
        raise HostedError(f"The hosted relay answered {status}.")
    return data
