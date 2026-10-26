"""Small Home Assistant WebSocket client for development scripts and integration tests.

Reads HA_WS and HA_TOKEN from the environment or the project's .env file.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import websockets

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


class HAError(Exception):
    pass


class HA:
    def __init__(self) -> None:
        load_env()
        self._url = os.environ["HA_WS"]
        self._token = os.environ["HA_TOKEN"]
        self._ws = None
        self._id = 0

    async def __aenter__(self) -> HA:
        self._ws = await websockets.connect(self._url, max_size=None)
        await self._ws.recv()
        await self._ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        if json.loads(await self._ws.recv()).get("type") != "auth_ok":
            raise HAError("authentication failed")
        return self

    async def __aexit__(self, *exc) -> None:
        await self._ws.close()

    async def call(self, command: dict[str, Any]) -> Any:
        self._id += 1
        await self._ws.send(json.dumps({"id": self._id, **command}))
        while True:
            msg = json.loads(await self._ws.recv())
            if msg.get("id") == self._id and msg.get("type") == "result":
                if not msg.get("success"):
                    raise HAError(msg.get("error"))
                return msg.get("result")

    async def supervisor(self, endpoint: str, method: str = "get", data: dict | None = None, timeout: int = 60) -> Any:
        command = {"type": "supervisor/api", "endpoint": endpoint, "method": method, "timeout": timeout}
        if data is not None:
            command["data"] = data
        return await self.call(command)


async def _cli(commands: list[str]) -> None:
    async with HA() as ha:
        for raw in commands:
            print(json.dumps(await ha.call(json.loads(raw)), indent=1))


if __name__ == "__main__":
    asyncio.run(_cli(sys.argv[1:]))
