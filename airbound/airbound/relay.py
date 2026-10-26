"""Relay mode: Home Assistant connects out to the user's own small server (VPS).

The VPS runs frps and accepts traffic for the public port. The add-on runs frpc,
which keeps an outgoing connection to it, so the home router needs no open ports.
Traffic stays encrypted end to end: frp only moves TLS bytes, and the certificate
lives at home. frpc adds a PROXY protocol header so Caddy knows the real client.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import secrets
import socket
from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout

from .settings import Settings

_LOGGER = logging.getLogger(__name__)

FRPC_CONFIG = Path("/data/frpc.toml")
INSTALL_TEMPLATE = Path(__file__).parent / "relay_install.sh"
RELAY_LISTENER = "127.0.0.1:8444"  # Caddy listener that expects a PROXY protocol header
ADMIN = "127.0.0.1:7400"  # frpc's local status API


def new_token() -> str:
    return secrets.token_hex(24)


def install_command(settings: Settings) -> str:
    """One line to paste on the VPS. The script is embedded, so nothing else is fetched from us."""
    script = (
        INSTALL_TEMPLATE.read_text()
        .replace("__TOKEN__", settings.relay_token)
        .replace("__CONTROL_PORT__", str(settings.relay_control_port))
        .replace("__PUBLIC_PORT__", str(settings.external_port))
    )
    encoded = base64.b64encode(script.encode()).decode()
    return f"echo {encoded} | base64 -d | sudo sh"


def render_frpc(settings: Settings) -> str:
    host, port = RELAY_LISTENER.split(":")
    admin_host, admin_port = ADMIN.split(":")
    return f"""serverAddr = "{settings.relay_host}"
serverPort = {settings.relay_control_port}
auth.method = "token"
auth.token = "{settings.relay_token}"
transport.tls.enable = true
loginFailExit = false
log.to = "console"
log.level = "warn"
webServer.addr = "{admin_host}"
webServer.port = {admin_port}

[[proxies]]
name = "airbound"
type = "tcp"
localIP = "{host}"
localPort = {port}
remotePort = {settings.external_port}
transport.proxyProtocolVersion = "v2"
"""


async def resolve_ipv4(host: str) -> str | None:
    """The relay's address for the DNS record (it may be given as a name or an IP)."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, family=socket.AF_INET)
    except OSError:
        return None
    return infos[0][4][0] if infos else None


class Frpc:
    """Runs frpc as a child process."""

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def apply(self, settings: Settings) -> None:
        await self.stop()
        FRPC_CONFIG.write_text(render_frpc(settings))
        os.chmod(FRPC_CONFIG, 0o600)
        self._process = await asyncio.create_subprocess_exec("frpc", "-c", str(FRPC_CONFIG))
        _LOGGER.info("Relay client started for %s", settings.relay_host)

    async def stop(self) -> None:
        if self.running:
            assert self._process is not None
            self._process.terminate()
            await self._process.wait()
        self._process = None

    async def status(self, session: ClientSession) -> dict:
        """{"state": "connected" | "connecting" | "stopped", "error": str}"""
        if not self.running:
            return {"state": "stopped", "error": ""}
        try:
            async with session.get(f"http://{ADMIN}/api/status", timeout=ClientTimeout(total=3)) as resp:
                data = await resp.json()
        except (ClientError, TimeoutError, ValueError):
            return {"state": "connecting", "error": ""}
        for proxy in data.get("tcp", []):
            if proxy.get("name") == "airbound":
                if proxy.get("status") == "running":
                    return {"state": "connected", "error": ""}
                return {"state": "connecting", "error": proxy.get("err", "")}
        return {"state": "connecting", "error": ""}


def explain(error: str) -> str:
    """frp errors in words a person can act on."""
    lowered = error.lower()
    if "token" in lowered or "authorization" in lowered:
        return "The relay rejected AirBound's key. Run the install command on the server again."
    if "port" in lowered and ("used" in lowered or "not allowed" in lowered):
        return "The relay can't use the public port. Something else on the server may use it."
    if error:
        return f"The relay reported: {error}"
    return ""
