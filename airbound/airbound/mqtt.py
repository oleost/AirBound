"""MQTT discovery: AirBound shows up in Home Assistant as a device with real entities.

Entities get unique IDs, so people can rename, hide and use them like any other.
Discovery messages and states are retained, so they survive a Home Assistant
restart. Device triggers ("New remote login" and so on) appear in the automation
editor under the device.

The broker's credentials come from the Supervisor (the add-on declares
`services: mqtt:want`), so nobody has to type anything. Without a broker,
reporting falls back to Home Assistant's REST API.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

import aiomqtt

from . import __version__

_LOGGER = logging.getLogger(__name__)

PREFIX = "homeassistant"
BASE = "airbound"
AVAILABILITY = f"{BASE}/availability"
DEVICE = {
    "identifiers": ["airbound_remote_access"],
    "name": "AirBound remote access",
    "manufacturer": "AirBound",
    "model": "Remote access add-on",
    "sw_version": __version__,
}

# key: (component, name, extra discovery fields)
ENTITIES: dict[str, tuple[str, str, dict[str, Any]]] = {
    "remote_access": ("binary_sensor", "Connected", {"device_class": "connectivity"}),
    "status": ("sensor", "Status", {"icon": "mdi:shield-check"}),
    "certificate_expires": ("sensor", "Certificate expires", {"device_class": "timestamp", "icon": "mdi:certificate"}),
    "blocked_addresses": ("sensor", "Blocked addresses", {"state_class": "measurement", "icon": "mdi:shield-lock"}),
    "refused_logins": ("sensor", "Logins refused (no MFA)", {"state_class": "total_increasing", "icon": "mdi:shield-alert"}),
    "failed_logins": ("sensor", "Failed remote logins", {"state_class": "total_increasing", "icon": "mdi:account-alert"}),
    "last_remote_login": ("sensor", "Last remote login", {"device_class": "timestamp", "icon": "mdi:login"}),
    "last_remote_connection": ("sensor", "Last remote connection", {"device_class": "timestamp", "icon": "mdi:web"}),
}
# Device triggers: (type, subtype) shown in the automation editor.
TRIGGERS = {
    "login": ("remote_login", "any"),
    "login_new_address": ("remote_login", "from_new_address"),
    "login_refused": ("remote_login", "refused_without_mfa"),
    "login_failed": ("remote_login", "failed"),
}


def entity_id(key: str) -> str:
    component, _, _ = ENTITIES[key]
    return f"{component}.airbound_{key}"


def discovery_messages(slug: str = "") -> list[tuple[str, dict]]:
    """(topic, payload) for every entity and trigger. The slug links the device to the AirBound panel."""
    device = {**DEVICE, "configuration_url": f"homeassistant://hassio/ingress/{slug}"} if slug else DEVICE
    messages = []
    for key, (component, name, extra) in ENTITIES.items():
        payload = {
            "name": name,
            "unique_id": f"airbound_{key}",
            "default_entity_id": entity_id(key),
            "state_topic": f"{BASE}/{key}/state",
            "json_attributes_topic": f"{BASE}/{key}/attributes",
            "availability_topic": AVAILABILITY,
            "device": device,
            **extra,
        }
        if component == "binary_sensor":
            payload.update(payload_on="on", payload_off="off")
        messages.append((f"{PREFIX}/{component}/airbound/{key}/config", payload))
    for key, (trigger_type, subtype) in TRIGGERS.items():
        messages.append(
            (
                f"{PREFIX}/device_automation/airbound/{key}/config",
                {
                    "automation_type": "trigger",
                    "type": trigger_type,
                    "subtype": subtype,
                    "topic": f"{BASE}/trigger/{key}",
                    "device": device,
                },
            )
        )
    return messages


def removal_topics() -> list[str]:
    """Every retained topic AirBound publishes. An empty retained message deletes each,
    and an empty discovery message makes Home Assistant remove the entity."""
    topics = [topic for topic, _ in discovery_messages()]
    for key in ENTITIES:
        topics += [f"{BASE}/{key}/state", f"{BASE}/{key}/attributes"]
    return topics + [AVAILABILITY]


class Mqtt:
    """Keeps a connection to the broker and publishes; reconnects by itself."""

    def __init__(self, config: dict, slug: str = "") -> None:
        self._config = config
        self._slug = slug
        self._client: aiomqtt.Client | None = None
        self._connected = asyncio.Event()
        self._task: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def running(self) -> bool:
        return self._task is not None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._client and self.connected:
            try:
                await self._client.publish(AVAILABILITY, "offline", retain=True)
            except aiomqtt.MqttError:
                pass
        if self._task:
            self._task.cancel()

    async def remove(self) -> None:
        """Take the device and its entities out of Home Assistant, then disconnect for good."""
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task  # a clean disconnect, so the broker doesn't send the will afterwards
            self._task = None
        cfg = self._config
        # Our own short connection, without a will: the running one would leave "offline" behind.
        async with aiomqtt.Client(
            hostname=cfg["host"],
            port=int(cfg.get("port", 1883)),
            username=cfg.get("username"),
            password=cfg.get("password"),
            identifier="airbound-remove",
        ) as client:
            for topic in removal_topics():
                await client.publish(topic, b"", retain=True)

    async def _run(self) -> None:
        cfg = self._config
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=cfg["host"],
                    port=int(cfg.get("port", 1883)),
                    username=cfg.get("username"),
                    password=cfg.get("password"),
                    identifier="airbound",
                    will=aiomqtt.Will(AVAILABILITY, "offline", retain=True),
                ) as client:
                    self._client = client
                    for topic, payload in discovery_messages(self._slug):
                        await client.publish(topic, json.dumps(payload), retain=True)
                    await client.publish(AVAILABILITY, "online", retain=True)
                    self._connected.set()
                    _LOGGER.info("Connected to MQTT; AirBound is a device in Home Assistant")
                    async for _ in client.messages:  # keeps the connection open
                        pass
            except aiomqtt.MqttError as err:
                _LOGGER.warning("MQTT connection lost (%s); retrying in 30 s", err)
            finally:
                self._connected.clear()
                self._client = None
            await asyncio.sleep(30)

    async def set(self, key: str, state: Any, attributes: dict | None = None) -> None:
        if not (self._client and self.connected):
            return
        value = "None" if state is None else str(state)
        await self._client.publish(f"{BASE}/{key}/state", value, retain=True)
        await self._client.publish(f"{BASE}/{key}/attributes", json.dumps(attributes or {}), retain=True)

    async def trigger(self, key: str, data: dict) -> None:
        if self._client and self.connected:
            await self._client.publish(f"{BASE}/trigger/{key}", json.dumps(data))
