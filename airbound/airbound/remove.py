"""Turning remote access off, and removing what AirBound changed before it is uninstalled.

Home Assistant gives an add-on no warning before it is uninstalled, so the
clean-up happens here, from the panel, while AirBound still runs. Everything
AirBound changed outside its own folder is undone where it can be; the rest is
listed for the person, in the order they'd do it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field

from aiohttp import ClientSession

from . import ha_settings, hosted, portmap, users
from .caddy import Caddy
from .cloudflare import Cloudflare
from .mqtt import ENTITIES, Mqtt, entity_id
from .relay import Frpc
from .settings import Settings, Store
from .supervisor import Supervisor

_LOGGER = logging.getLogger(__name__)
RESTART_TIMEOUT = 600  # Home Assistant restarts to try the restored settings
NOTIFICATIONS = ("status", "mfa_limited", "certificate", "unexpected_certificate")


@dataclass
class Task:
    id: str
    label: str
    state: str = "waiting"  # waiting | running | done | error
    detail: str = ""


@dataclass
class Plan:
    tasks: list[Task] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # left for the person to do


def plan(s: Settings, http: dict, people: list[dict]) -> Plan:
    """What removing does, from the settings, HA's stable HTTP settings and its users."""
    result = Plan()
    tasks = result.tasks
    tasks.append(Task("stop", "Stop remote access"))
    if any(p["id"] in s.auto_limited and p["local_only"] for p in people):
        tasks.append(Task("users", "Let out the people AirBound limited to the home network"))
    if s.tls == "cloudflare" and s.cloudflare_token and s.zone_id and s.fqdn:
        tasks.append(Task("dns", f"Delete {s.fqdn} from Cloudflare"))
    if s.port_mapping:
        tasks.append(Task("router", f"Close port {s.external_port} on your router"))
    if s.hosted_key:
        tasks.append(Task("hosted", "Unlink this Home Assistant from airbound.dev"))
    tasks.append(Task("sensors", "Remove AirBound's device, sensors and notifications"))
    if ha_settings.restored(http, s.ha_http_before) is not None:
        tasks.append(Task("ha_settings", "Put Home Assistant's network settings back (Home Assistant restarts)"))
    tasks.append(Task("forget", "Forget AirBound's settings and keys"))

    notes = result.notes
    if s.mode == "direct" and not s.port_mapping and (s.router_done or s.live):
        notes.append(f"Remove the port forward for TCP port {s.external_port} from your router.")
    if s.mode == "relay" and s.relay_host:
        notes.append(
            f"Your relay server {s.relay_host} keeps running, and costs money until you delete it at your hosting provider. "
            "Nobody can use the relay without the keys AirBound forgets now. If you keep the server for something else, "
            "AirBound's set-up stays on it: the relay, a firewall, SSH with keys only "
            "and automatic security updates."
        )
    if s.cloudflare_token:
        notes.append("Delete the API token you made for AirBound in Cloudflare: My Profile → API Tokens.")
    if s.mode == "hosted" or s.hosted_key:
        notes.append("Your Patreon or GitHub membership goes on until you cancel it there.")
    unknown = ha_settings.unknown(http, s.ha_http_before)
    if unknown:
        listed = ", ".join(f"{u['label']}: {u['current']}" for u in unknown)
        notes.append(
            f"These Home Assistant network settings were set before AirBound remembered the old values, so they stay: {listed}. "
            "Other apps, such as a reverse proxy, may need them. Change them under Settings → System → Network → HTTP server if not."
        )
    return result


class Removal:
    def __init__(
        self,
        store: Store,
        supervisor: Supervisor,
        session: ClientSession,
        caddy: Caddy,
        frpc: Frpc,
        broker: Mqtt | None,
    ) -> None:
        self._store = store
        self._supervisor = supervisor
        self._session = session
        self._caddy = caddy
        self._frpc = frpc
        self._broker = broker
        self.tasks: list[Task] = []
        self._running: asyncio.Task | None = None

    @property
    def busy(self) -> bool:
        return self._running is not None and not self._running.done()

    async def _http(self) -> dict:
        (state,) = await self._supervisor.ws({"type": "http/config"})
        return state["stable"]

    async def plan(self) -> Plan:
        return plan(self._store.settings, await self._http(), await users.list_people(self._supervisor, None))

    async def pause(self) -> list[str]:
        """Remote access off, everything else kept; returns the people let out again."""
        await self._caddy.stop()
        await self._frpc.stop()
        self._store.update(live=False, paused=True)
        _, released = await users.enforce_mfa(self._supervisor, self._store)
        return released

    def progress(self) -> dict:
        return {"busy": self.busy, "tasks": [asdict(t) for t in self.tasks]}

    async def start(self) -> None:
        if self.busy:
            return
        planned = await self.plan()
        self.tasks = planned.tasks
        self._running = asyncio.create_task(self._run(planned.notes))

    async def _run(self, notes: list[str]) -> None:
        current = None
        try:
            for task in self.tasks:
                current = task
                task.state = "running"
                note = await getattr(self, f"_{task.id}")(task)
                if note:
                    notes.append(note)
                task.state = "done"
            self._store.update(removed_notes=tuple(notes))
        except Exception as err:  # every failure is shown in the UI
            _LOGGER.exception("Removing failed")
            if current:
                current.state = "error"
                current.detail = str(err) or f"Unexpected error: {type(err).__name__}"

    async def _stop(self, task: Task) -> None:
        await self._caddy.stop()
        await self._frpc.stop()
        self._store.update(live=False)

    async def _users(self, task: Task) -> None:
        _, released = await users.enforce_mfa(self._supervisor, self._store)
        task.detail = ", ".join(released)

    async def _dns(self, task: Task) -> str | None:
        s = self._store.settings
        try:
            count = await Cloudflare(self._session, s.cloudflare_token).delete_records(s.zone_id, s.fqdn)
        except Exception as err:  # the person can do this by hand; don't stop here
            task.detail = str(err)
            return f"Delete the DNS records for {s.fqdn} (A and CAA) in Cloudflare."
        task.detail = f"{count} DNS record{'s' if count != 1 else ''} deleted"
        return None

    async def _router(self, task: Task) -> str | None:
        s = self._store.settings
        try:
            gateway = await portmap.gateway_of(self._supervisor)
            if not gateway:
                raise portmap.PortMapError("no router address")
            await portmap.close_port(self._session, gateway, s.external_port, s.port_mapping)
        except portmap.PortMapError as err:
            task.detail = f"The router didn't answer ({err})"
            return f"Your router closes port {s.external_port} by itself within an hour, now that AirBound stops renewing it."
        task.detail = f"Port {s.external_port} closed"
        return None

    async def _hosted(self, task: Task) -> str | None:
        try:
            await hosted.unlink(self._session, self._store.settings.hosted_key)
        except hosted.HostedError as err:
            task.detail = str(err)
            return "airbound.dev couldn't be told. The address stays reserved until your membership ends."
        s = self._store.settings
        # Linked once, then switched to another mode: the airbound.dev address isn't the current one.
        task.detail = f"{s.fqdn} is free again" if s.tls == "hosted" else "Your airbound.dev address is free again"
        return None

    async def _sensors(self, task: Task) -> None:
        if self._broker:
            await self._broker.remove()
        for key in ENTITIES:  # states made through the REST API when there was no MQTT broker
            await self._supervisor.core_delete(f"/states/{entity_id(key)}")
        for notification in NOTIFICATIONS:
            await self._supervisor.core_post("/services/persistent_notification/dismiss", {"notification_id": f"airbound_{notification}"})

    async def _ha_settings(self, task: Task) -> None:
        config = ha_settings.restored(await self._http(), self._store.settings.ha_http_before)
        if config is None:
            return
        await self._supervisor.ws({"type": "http/config/configure", "config": config})
        # Home Assistant restarts and tries the settings; they stay once promoted.
        loop = asyncio.get_running_loop()
        started = loop.time()
        restarted = False
        while loop.time() < started + RESTART_TIMEOUT:
            await asyncio.sleep(5)
            try:
                (state,) = await self._supervisor.ws({"type": "http/config"})
            except Exception:  # down while it restarts
                restarted = True
                continue
            pending = state.get("pending")
            if pending and pending.get("error"):
                raise RuntimeError(f"Home Assistant didn't accept its old network settings: {pending.get('error_message')}")
            # Promote only what runs after the restart (or once it's clearly done, if it was too quick to see).
            if pending and (restarted or loop.time() > started + 120):
                await self._supervisor.ws({"type": "http/config/promote"})
                task.detail = "Back as they were before AirBound"
                return
            if ha_settings.restored(state["stable"], self._store.settings.ha_http_before) is None:
                task.detail = "Back as they were before AirBound"
                return
        raise RuntimeError("Home Assistant didn't come back within 10 minutes. Open AirBound again and retry.")

    async def _forget(self, task: Task) -> None:
        # Keep only what tells the panel it was removed; uninstalling deletes the rest.
        self._store.reset(removed=True, install_id=self._store.settings.install_id)

    def set_up_again(self) -> None:
        self.tasks = []
        self._store.reset(install_id=self._store.settings.install_id)
        if self._broker and not self._broker.running:
            self._broker.start()  # the device and its sensors come back
