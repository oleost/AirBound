"""Automatic port opening on the router, for routers that allow it.

Tries NAT-PMP (RFC 6886) first, then UPnP IGD. Both only work when the router
has UPnP/NAT-PMP turned on, which many security guides advise against because
any device on the network can then open ports. The add-on's requests leave
through Home Assistant's own address, so the router maps the port to it.

UPnP discovery is sent straight to the router instead of multicast, because
multicast doesn't leave the add-on's network.
"""

from __future__ import annotations

import asyncio
import logging
import re
import struct
from xml.etree import ElementTree

from aiohttp import ClientError, ClientSession, ClientTimeout

_LOGGER = logging.getLogger(__name__)

LEASE = 3600  # seconds; renewed well before it runs out
DESCRIPTION = "AirBound (Home Assistant)"
WAN_SERVICES = (
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)


class PortMapError(Exception):
    pass


# ---------- NAT-PMP ----------


def natpmp_request(external_port: int, internal_port: int, lifetime: int) -> bytes:
    """Version 0, opcode 2 (map TCP)."""
    return struct.pack("!BBHHHI", 0, 2, 0, internal_port, external_port, lifetime)


def natpmp_parse(data: bytes) -> int:
    """The external port the router mapped; raises if it refused."""
    if len(data) < 16:
        raise PortMapError("short NAT-PMP answer")
    _version, opcode, result, _epoch, _internal, external, _lifetime = struct.unpack("!BBHIHHI", data[:16])
    if opcode != 130:
        raise PortMapError(f"unexpected NAT-PMP opcode {opcode}")
    if result != 0:
        raise PortMapError(f"router refused (NAT-PMP result {result})")
    return external


class _UDP(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.answer: asyncio.Future = asyncio.get_running_loop().create_future()

    def datagram_received(self, data: bytes, addr) -> None:
        if not self.answer.done():
            self.answer.set_result(data)


async def _udp_ask(host: str, port: int, payload: bytes, timeout: float) -> bytes:
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(_UDP, remote_addr=(host, port))
    try:
        for attempt in range(3):  # RFC 6886: retry with growing delays
            transport.sendto(payload)
            try:
                return await asyncio.wait_for(asyncio.shield(protocol.answer), timeout * (attempt + 1))
            except TimeoutError:
                continue
        raise PortMapError("no answer")
    finally:
        transport.close()


async def natpmp_map(gateway: str, external_port: int, internal_port: int, lifetime: int = LEASE) -> int:
    data = await _udp_ask(gateway, 5351, natpmp_request(external_port, internal_port, lifetime), 0.5)
    return natpmp_parse(data)


# ---------- UPnP IGD ----------


def ssdp_location(answer: str) -> str | None:
    match = re.search(r"^location:\s*(\S+)", answer, re.IGNORECASE | re.MULTILINE)
    return match.group(1) if match else None


def find_control_url(description: str, base: str) -> tuple[str, str] | None:
    """(control URL, service type) of the router's WAN connection service."""
    if "<!DOCTYPE" in description.upper() or "<!ENTITY" in description.upper():
        return None  # a router description never needs a DTD; refuse entity tricks outright
    root = ElementTree.fromstring(description)
    for service in root.iter():
        if not service.tag.endswith("service"):
            continue
        fields = {child.tag.split("}")[-1]: (child.text or "").strip() for child in service}
        if fields.get("serviceType") in WAN_SERVICES and fields.get("controlURL"):
            url = fields["controlURL"]
            if not url.startswith("http"):
                url = base.rstrip("/") + "/" + url.lstrip("/")
            return url, fields["serviceType"]
    return None


def soap_body(service: str, action: str, args: dict[str, str | int]) -> str:
    params = "".join(f"<{k}>{v}</{k}>" for k, v in args.items())
    return (
        '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action} xmlns:u="{service}">{params}</u:{action}></s:Body></s:Envelope>'
    )


async def upnp_map(session: ClientSession, gateway: str, external_port: int, internal_client: str, internal_port: int) -> int:
    search = (
        "M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\n"
        "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n"
    ).encode()
    answer = (await _udp_ask(gateway, 1900, search, 1.0)).decode(errors="replace")
    location = ssdp_location(answer)
    if not location:
        raise PortMapError("router gave no UPnP description")
    timeout = ClientTimeout(total=5)
    try:
        async with session.get(location, timeout=timeout) as resp:
            description = await resp.text()
        found = find_control_url(description, re.match(r"https?://[^/]+", location).group(0))
        if not found:
            raise PortMapError("router has no UPnP port mapping service")
        url, service = found
        body = soap_body(
            service,
            "AddPortMapping",
            {
                "NewRemoteHost": "",
                "NewExternalPort": external_port,
                "NewProtocol": "TCP",
                "NewInternalPort": internal_port,
                "NewInternalClient": internal_client,
                "NewEnabled": 1,
                "NewPortMappingDescription": DESCRIPTION,
                "NewLeaseDuration": LEASE,
            },
        )
        headers = {"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{service}#AddPortMapping"'}
        async with session.post(url, data=body, headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                raise PortMapError(f"router refused (UPnP HTTP {resp.status})")
    except (ClientError, TimeoutError, ElementTree.ParseError) as err:
        raise PortMapError(f"UPnP failed: {err}") from err
    return external_port


# ---------- both ----------


async def open_port(session: ClientSession, gateway: str, ha_ip: str, external_port: int, internal_port: int = 443) -> str:
    """Open the port with whatever the router supports. Returns "natpmp" or "upnp"."""
    errors = []
    try:
        mapped = await natpmp_map(gateway, external_port, internal_port)
        if mapped != external_port:
            await natpmp_map(gateway, mapped, internal_port, 0)  # undo; we need the exact port
            raise PortMapError(f"router offered port {mapped} instead of {external_port}")
        return "natpmp"
    except (PortMapError, OSError) as err:
        errors.append(f"NAT-PMP: {err}")
    try:
        await upnp_map(session, gateway, external_port, ha_ip, internal_port)
        return "upnp"
    except (PortMapError, OSError) as err:
        errors.append(f"UPnP: {err}")
    _LOGGER.info("Automatic port opening failed: %s", "; ".join(errors))
    raise PortMapError("; ".join(errors))


async def gateway_of(supervisor) -> str | None:
    info = await supervisor.get("/network/info")
    for interface in info.get("interfaces", []):
        if interface.get("primary") and interface.get("ipv4", {}).get("gateway"):
            return interface["ipv4"]["gateway"]
    return None

