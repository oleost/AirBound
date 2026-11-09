"""Hosted mode: Home Assistant connects out to AirBound's shared relay.

Linking works like signing in a TV: AirBound asks the relay's website for a link
request, shows a short code and a link, the person logs in with Patreon there,
and AirBound picks up its own key and address. The relay routes connections by
name without decrypting them; the certificate is made here, at home.

Every call with the device key also sends this machine's key (machine.py):
the relay answers only the machine that first used the device key, so a key
copied out of a backup can't read or unlink the address.

The relay's certificate authority is pinned: the CA shipped with the add-on is
the only one frpc trusts, whatever the website sends.

What the website answers is checked before it is used (checked_device,
checked_address): the address and key end up in Caddy's and frpc's
configuration, so a website that was taken over must not be able to write
anything but a plain name under its own domain there.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout

from .settings import valid_hostname

SERVICE = os.environ.get("AIRBOUND_HOSTED_SERVICE", "https://airbound.dev")
RELAY_HOST = os.environ.get("AIRBOUND_HOSTED_RELAY", "relay.airbound.dev")
RELAY_PORT = 7000
SERVER_NAME = "airbound-hosted-relay"  # the name in the relay's certificate
AUTH_TOKEN = "airbound-hosted-relay"  # not a secret: the device key is what counts
CA_FILE = Path(__file__).parent / "hosted_relay_ca.pem"
TIMEOUT = ClientTimeout(total=15)


DOMAIN = (urlsplit(SERVICE).hostname or "").lower()  # addresses are always under the website's own domain
_PLAIN = re.compile(r"[A-Za-z0-9_-]{1,128}")


class HostedError(Exception):
    pass


def plain(value: object) -> bool:
    """Only letters, digits, dashes and underscores: safe inside a configuration file."""
    return isinstance(value, str) and bool(_PLAIN.fullmatch(value))


def checked_address(hostname: object, domain: object) -> tuple[str, str]:
    """(hostname, domain) if it is one plain name under the hosted relay's own domain."""
    if not (isinstance(hostname, str) and isinstance(domain, str) and valid_hostname(hostname) and domain.lower() == DOMAIN):
        raise HostedError("The hosted relay sent an address AirBound doesn't accept. Nothing was changed; try again later.")
    return hostname, DOMAIN


def checked_device(device: object) -> dict:
    """{id, key, hostname, domain} from a finished link request, refused unless every value is plain."""
    if not isinstance(device, dict) or not (plain(device.get("id")) and plain(device.get("key"))):
        raise HostedError("The hosted relay sent a key AirBound doesn't accept. Nothing was changed; try again later.")
    hostname, domain = checked_address(device.get("hostname"), device.get("domain"))
    return {"id": device["id"], "key": device["key"], "hostname": hostname, "domain": domain}


def web_link(url: object) -> str:
    """A link from the hosted relay for the panel to show; only https, so it can't run script."""
    return url if isinstance(url, str) and url.startswith("https://") and len(url) < 500 else ""


async def _call(
    session: ClientSession, method: str, path: str, token: str = "", body: dict | None = None, machine: str = ""
) -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if machine:
        headers["X-AirBound-Machine"] = machine
    try:
        async with session.request(method, f"{SERVICE}{path}", json=body, headers=headers, timeout=TIMEOUT) as resp:
            try:
                data = await resp.json(content_type=None)
            except ValueError:  # an error page; the status says enough
                data = {}
            return resp.status, data if isinstance(data, dict) else {}
    except (ClientError, TimeoutError) as err:
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


ACCOUNT_CACHE = 600  # the health checks run every few seconds while the panel is open
_accounts: dict[str, tuple[float, dict]] = {}


async def account(session: ClientSession, key: str, machine: str) -> dict:
    """{state: active | grace | expired | unlinked | other_machine, fqdn, grace_until, join_url, manage_url, foreign_seen}

    foreign_seen: when another machine last tried this key, or None.
    """
    cached = _accounts.get(key)
    if cached and time.monotonic() - cached[0] < ACCOUNT_CACHE:
        return cached[1]
    status, data = await _call(session, "GET", "/api/v1/device", token=key, machine=machine)
    if status == 404:
        data = {"state": "unlinked"}
    elif status == 409:
        data = {"state": "other_machine"}
    elif status != 200:
        raise HostedError(f"The hosted relay answered {status}.")
    _accounts.clear()  # only the current key matters
    _accounts[key] = (time.monotonic(), data)
    return data


async def unlink(session: ClientSession, key: str, machine: str) -> None:
    """Tell the relay this Home Assistant is gone, so its address and device slot are freed."""
    status, _ = await _call(session, "DELETE", "/api/v1/device", token=key, machine=machine)
    if status == 409:
        raise HostedError("airbound.dev links this address to another Home Assistant, so it stays with that one.")
    if status not in (200, 404):  # 404: already unlinked
        raise HostedError(f"The hosted relay answered {status}.")
    _accounts.clear()
