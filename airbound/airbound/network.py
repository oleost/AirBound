"""Addresses and reachability."""

from __future__ import annotations

import asyncio
import ipaddress
import ssl

from aiohttp import ClientError, ClientSession, ClientTimeout
from cryptography import x509
from cryptography.x509.oid import NameOID

from .supervisor import Supervisor

PUBLIC_IP_SERVICES = ("https://api.ipify.org", "https://icanhazip.com", "https://ifconfig.me/ip")


async def public_ip(session: ClientSession) -> str | None:
    for url in PUBLIC_IP_SERVICES:
        try:
            async with session.get(url, timeout=ClientTimeout(total=5)) as resp:
                ip = (await resp.text()).strip()
            if ipaddress.ip_address(ip).version == 4:
                return ip
        except (ClientError, TimeoutError, ValueError):
            continue
    return None


async def ha_ip(supervisor: Supervisor) -> str | None:
    """Home Assistant's address on the home network, for the port forward."""
    info = await supervisor.get("/network/info")
    for interface in info.get("interfaces", []):
        if interface.get("primary") and interface.get("ipv4", {}).get("address"):
            return interface["ipv4"]["address"][0].split("/")[0]
    return None


async def tls_reachable(host: str, server_name: str, verify: bool, port: int = 443, timeout: float = 6) -> bool:
    """Can a TLS connection to host:port be made for server_name?"""
    context = ssl.create_default_context()
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=context, server_hostname=server_name), timeout
        )
    except (OSError, TimeoutError, ssl.SSLError):
        return False
    writer.close()
    return True


async def peer_certificate(host: str, port: int, server_name: str, timeout: float = 6) -> bytes | None:
    """The certificate a server presents, without checking it. None if nothing speaks TLS there."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=context, server_hostname=server_name), timeout
        )
    except (OSError, TimeoutError, ssl.SSLError):
        return None
    cert = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
    writer.close()
    return cert


async def port_open(host: str, port: int, timeout: float = 4) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, TimeoutError):
        return False
    writer.close()
    return True


def certificate_owner(der: bytes) -> str:
    """A readable name for whoever a certificate belongs to, e.g. "UniFi (Ubiquiti Inc.)"."""
    try:
        subject = x509.load_der_x509_certificate(der).subject
    except ValueError:
        return ""
    names = {attr.oid: str(attr.value) for attr in subject}
    common, org = names.get(NameOID.COMMON_NAME, ""), names.get(NameOID.ORGANIZATION_NAME, "")
    if common and org and org not in common:
        return f"{common} ({org})"
    return common or org


async def who_answers(public: str, port: int, server_name: str) -> tuple[str, str]:
    """What answers on the home's public address, seen from inside through the router.

    Returns (answer, owner):
    "airbound": AirBound itself, so the port forward works.
    "other": another server or device answers there; owner names it when it can.
    "unknown": nothing answered. Many routers can't loop back to their own public
    address, so this doesn't mean the port forward is broken.
    """
    ours = await peer_certificate("127.0.0.1", 443, server_name)
    theirs = await peer_certificate(public, port, server_name)
    if theirs is not None:
        return ("airbound", "") if theirs == ours else ("other", certificate_owner(theirs))
    return ("other", "") if await port_open(public, port) else ("unknown", "")
