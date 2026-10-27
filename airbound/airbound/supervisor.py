"""Clients for the Supervisor API and Home Assistant's WebSocket API."""

from __future__ import annotations

import itertools
import os
import time
from typing import Any

from aiohttp import ClientSession, WSMsgType

SUPERVISOR_URL = "http://supervisor"
CORE_WS_URL = "ws://supervisor/core/websocket"
UPSTREAM_CACHE = 60  # Home Assistant's port only changes with a restart


class SupervisorError(Exception):
    """Raised when the Supervisor or Home Assistant returns an error."""


class Supervisor:
    def __init__(self, session: ClientSession, token: str | None = None) -> None:
        self._session = session
        self._token = token or os.environ.get("SUPERVISOR_TOKEN", "")
        self._ids = itertools.count(1)
        self._upstream: tuple[float, str] | None = None

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def get(self, path: str) -> Any:
        async with self._session.get(f"{SUPERVISOR_URL}{path}", headers=self._headers) as resp:
            body = await resp.json()
        if body.get("result") != "ok":
            raise SupervisorError(body.get("message", f"GET {path} failed"))
        return body.get("data")

    async def core_post(self, path: str, data: dict) -> None:
        """POST to Home Assistant's REST API (states, events, services) through the Supervisor."""
        async with self._session.post(f"{SUPERVISOR_URL}/core/api{path}", headers=self._headers, json=data) as resp:
            if resp.status >= 400:
                raise SupervisorError(f"POST {path} failed: {resp.status}")

    async def core_delete(self, path: str) -> None:
        async with self._session.delete(f"{SUPERVISOR_URL}/core/api{path}", headers=self._headers) as resp:
            if resp.status >= 400 and resp.status != 404:
                raise SupervisorError(f"DELETE {path} failed: {resp.status}")

    async def mqtt_service(self) -> dict | None:
        """The MQTT broker's address and credentials, if one is installed."""
        try:
            return await self.get("/services/mqtt")
        except SupervisorError:
            return None

    async def ha_upstream(self) -> str:
        """host:port where Caddy and the guard reach Home Assistant."""
        if self._upstream and time.monotonic() - self._upstream[0] < UPSTREAM_CACHE:
            return self._upstream[1]
        info = await self.get("/core/info")
        upstream = f"homeassistant:{info['port']}"
        self._upstream = (time.monotonic(), upstream)
        return upstream

    async def ws(self, *commands: dict[str, Any]) -> list[Any]:
        """Run Home Assistant WebSocket commands in order and return their results."""
        results = []
        async with self._session.ws_connect(CORE_WS_URL) as ws:
            await ws.receive_json()  # auth_required
            await ws.send_json({"type": "auth", "access_token": self._token})
            if (await ws.receive_json()).get("type") != "auth_ok":
                raise SupervisorError("Home Assistant rejected the Supervisor token")
            for command in commands:
                msg_id = next(self._ids)
                await ws.send_json({"id": msg_id, **command})
                while True:
                    msg = await ws.receive()
                    if msg.type != WSMsgType.TEXT:
                        raise SupervisorError("WebSocket closed unexpectedly")
                    data = msg.json()
                    if data.get("id") == msg_id and data.get("type") == "result":
                        break
                if not data.get("success"):
                    raise SupervisorError(data.get("error", {}).get("message", "command failed"))
                results.append(data.get("result"))
        return results
