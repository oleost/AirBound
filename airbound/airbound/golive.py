"""Going live: DNS record, certificate and proxy, with progress the UI can show."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from dataclasses import asdict, dataclass

from aiohttp import ClientSession

from . import hosted, network, portmap, relay
from .caddy import Caddy
from .cloudflare import Cloudflare, CloudflareError
from .relay import Frpc
from .settings import Store

_LOGGER = logging.getLogger(__name__)
CERT_TIMEOUT = 180  # DNS-01 usually takes 20-90 seconds
RELAY_TIMEOUT = 30
DDNS_INTERVAL = 300
PORTMAP_INTERVAL = 1800  # leases last an hour


@dataclass
class Task:
    id: str
    label: str
    state: str = "waiting"  # waiting | running | done | error
    detail: str = ""


class GoLive:
    def __init__(self, store: Store, caddy: Caddy, frpc: Frpc, session: ClientSession, ha_upstream: str) -> None:
        self._store = store
        self._caddy = caddy
        self._frpc = frpc
        self._session = session
        self._ha_upstream = ha_upstream
        self.tasks: list[Task] = []
        self._running: asyncio.Task | None = None

    @property
    def busy(self) -> bool:
        return self._running is not None and not self._running.done()

    def progress(self) -> dict:
        return {"busy": self.busy, "tasks": [asdict(t) for t in self.tasks]}

    def start(self) -> None:
        if self.busy or self._store.settings.restored:
            return
        settings = self._store.settings
        relay = settings.mode == "relay"
        self.tasks = []
        if settings.tls == "cloudflare":
            target = "your relay server" if relay else "your home"
            self.tasks.append(Task("dns", f"Point {settings.fqdn} to {target}"))
        if settings.mode == "hosted":
            # The certificate is checked through the relay, so the tunnel comes first.
            self.tasks += [
                Task("proxy", "Start the secure proxy"),
                Task("relay", "Connect to AirBound's hosted relay"),
                Task("certificate", "Get a certificate"),
            ]
        else:
            self.tasks += [Task("proxy", "Start the secure proxy"), Task("certificate", "Get a certificate")]
            if relay:
                self.tasks.append(Task("relay", "Connect to your relay server"))
        self._running = asyncio.create_task(self._run())

    async def _run(self) -> None:
        current = None
        try:
            for task in self.tasks:
                current = task
                task.state = "running"
                await getattr(self, f"_{task.id}")(task)
                task.state = "done"
            self._store.update(live=True, paused=False)
        except Exception as err:  # every failure is shown in the UI
            _LOGGER.exception("Go live failed")
            if current:
                current.state = "error"
                current.detail = str(err) if isinstance(err, (CloudflareError, RuntimeError)) else f"Unexpected error: {err}"

    async def _dns(self, task: Task) -> None:
        settings = self._store.settings
        if settings.mode == "relay":
            ip = await relay.resolve_ipv4(settings.relay_host)
            if not ip:
                raise RuntimeError(f"AirBound couldn't find the address of {settings.relay_host}. Check the relay address.")
            if not ipaddress.ip_address(ip).is_global:
                # A public name pointing at a private address breaks remote access for everyone.
                raise RuntimeError(
                    f"The relay address {ip} is a private address, so nobody outside your network could reach it. "
                    "Enter the relay server's public IP address."
                )
        else:
            ip = await network.public_ip(self._session, fresh=True)
            if not ip:
                raise RuntimeError("AirBound couldn't find your internet address. Check that Home Assistant has internet access.")
        cloudflare = Cloudflare(self._session, settings.cloudflare_token)
        await cloudflare.upsert_a_record(settings.zone_id, settings.fqdn, ip)
        await cloudflare.lock_certificates(settings.zone_id, settings.fqdn)
        self._store.update(public_ip=ip)
        task.detail = f"{settings.fqdn} → {ip}, certificates locked to AirBound"

    async def _proxy(self, task: Task) -> None:
        await self._caddy.apply(self._store.settings, self._ha_upstream)
        if self._store.settings.mode not in ("relay", "hosted"):
            await self._frpc.stop()

    async def _relay(self, task: Task) -> None:
        settings = self._store.settings
        host = hosted.RELAY_HOST if settings.mode == "hosted" else settings.relay_host
        await self._frpc.apply(settings)
        status = {}
        for _ in range(RELAY_TIMEOUT // 2):
            status = await self._frpc.status(self._session)
            if status["state"] == "connected":
                task.detail = f"Connected to {host}"
                return
            await asyncio.sleep(2)
        if settings.mode == "hosted":
            raise RuntimeError(
                relay.explain(status.get("error", ""))
                or f"AirBound couldn't reach the hosted relay at {host}. Check that Home Assistant has internet access, then try again."
            )
        raise RuntimeError(
            relay.explain(status.get("error", ""))
            or f"AirBound couldn't reach the relay at {self._store.settings.relay_host}. "
            "Check that the install command finished and that the server allows port "
            f"{self._store.settings.relay_control_port}."
        )

    async def _certificate(self, task: Task) -> None:
        settings = self._store.settings
        for _ in range(CERT_TIMEOUT // 3):
            if await network.tls_reachable("127.0.0.1", settings.fqdn, verify=settings.tls != "internal"):
                task.detail = "Valid certificate for your domain"
                return
            await asyncio.sleep(3)
        if settings.mode == "hosted":
            raise RuntimeError("No certificate after 3 minutes. Let's Encrypt may be busy; try again in a few minutes.")
        raise RuntimeError(
            "No certificate after 3 minutes. Check that the Cloudflare token can edit DNS for this domain, then try again."
        )


async def start_saved(store: Store, caddy: Caddy, frpc: Frpc, ha_upstream: str) -> None:
    """Remote access as it was set up, after a restart or a confirmed restore."""
    await caddy.apply(store.settings, ha_upstream)
    if store.settings.mode in ("relay", "hosted"):
        await frpc.apply(store.settings)


async def keep_dns_current(store: Store, session: ClientSession) -> None:
    """Built-in dynamic DNS: follow the home's public IP if the ISP changes it."""
    while True:
        await asyncio.sleep(DDNS_INTERVAL)
        settings = store.settings
        if not (settings.live and settings.tls == "cloudflare") or settings.mode == "relay" or settings.restored:
            continue  # in relay mode the record points to the relay, which doesn't move
        try:
            ip = await network.public_ip(session, fresh=True)
            if ip and ip != settings.public_ip:
                await Cloudflare(session, settings.cloudflare_token).upsert_a_record(settings.zone_id, settings.fqdn, ip)
                store.update(public_ip=ip)
                _LOGGER.info("Internet address changed; %s now points to %s", settings.fqdn, ip)
        except Exception:
            _LOGGER.exception("Couldn't update the DNS record")


async def keep_port_open(store: Store, session: ClientSession, supervisor) -> None:
    """Renew an automatic port mapping before the router's lease runs out."""
    while True:
        await asyncio.sleep(PORTMAP_INTERVAL)
        settings = store.settings
        if not (settings.port_mapping and settings.mode == "direct" and settings.live) or settings.restored:
            continue
        try:
            gateway = await portmap.gateway_of(supervisor)
            ha_ip = await network.ha_ip(supervisor)
            if gateway and ha_ip:
                await portmap.open_port(session, gateway, ha_ip, settings.external_port)
        except portmap.PortMapError as err:
            _LOGGER.warning("Couldn't renew the router port mapping: %s", err)
