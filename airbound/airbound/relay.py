"""Relay mode: Home Assistant connects out to the user's own small server (VPS).

The VPS runs frps and accepts traffic for the public port. The add-on runs frpc,
which keeps an outgoing connection to it, so the home router needs no open ports.
Traffic stays encrypted end to end: frp only moves TLS bytes, and the certificate
lives at home. frpc adds a PROXY protocol header so Caddy knows the real client.

The tunnel itself is pinned: AirBound makes a small private CA and a server
certificate for the relay, ships both inside the install command, and frpc only
trusts that CA. Without it, anyone between Home Assistant and the relay could
pose as the relay and open connections into Home Assistant with made-up client
addresses.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import gzip
import ipaddress
import logging
import os
import re
import secrets
import socket
from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from . import hosted
from .settings import Settings

_LOGGER = logging.getLogger(__name__)

FRPC_CONFIG = Path("/data/frpc.toml")
INSTALL_TEMPLATE = Path(__file__).parent / "relay_install.sh"
HARDEN_SCRIPT = Path(__file__).parent / "vps_harden.sh"
RELAY_LISTENER = "127.0.0.1:8444"  # Caddy listener that expects a PROXY protocol header
ADMIN = "127.0.0.1:7400"  # frpc's local status API
PKI_DIR = Path("/data/relay")
SERVER_NAME = "airbound-relay"  # the name frpc expects in the relay's certificate
HOST_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")


def new_token() -> str:
    return secrets.token_hex(24)


def valid_relay_host(host: str) -> bool:
    """An IP address or a DNS name: nothing that could break out of the frpc config."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(HOST_RE.match(host))


def _pem(obj) -> str:
    if isinstance(obj, x509.Certificate):
        return obj.public_bytes(serialization.Encoding.PEM).decode()
    return obj.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def make_pki() -> dict[str, str]:
    """A private CA and a server certificate for the relay, as PEM strings."""
    now = datetime.datetime.now(datetime.UTC)
    later = now + datetime.timedelta(days=3650)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "AirBound relay CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(later)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    server_key = ec.generate_private_key(ec.SECP256R1())
    server = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, SERVER_NAME)]))
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(later)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(SERVER_NAME)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    return {"ca.crt": _pem(ca), "server.crt": _pem(server), "server.key": _pem(server_key)}


def ensure_pki(directory: Path = PKI_DIR, renew: bool = False) -> dict[str, str]:
    """Load the relay certificates, creating them the first time (or when renewing the key)."""
    files = ("ca.crt", "server.crt", "server.key")
    if renew or not all((directory / f).exists() for f in files):
        directory.mkdir(parents=True, exist_ok=True)
        for name, pem in make_pki().items():
            path = directory / name
            path.write_text(pem)
            os.chmod(path, 0o600)
    return {f: (directory / f).read_text() for f in files}


def install_command(settings: Settings, pki: dict[str, str]) -> str:
    """One line to paste on the VPS. The script is embedded, so nothing else is fetched from us."""
    script = (
        INSTALL_TEMPLATE.read_text()
        .replace("__TOKEN__", settings.relay_token)
        .replace("__CONTROL_PORT__", str(settings.relay_control_port))
        .replace("__PUBLIC_PORT__", str(settings.external_port))
        .replace("__SERVER_CERT__", pki["server.crt"].strip())
        .replace("__SERVER_KEY__", pki["server.key"].strip())
        .replace("__HARDEN__", HARDEN_SCRIPT.read_text())
    )
    encoded = base64.b64encode(gzip.compress(script.encode(), mtime=0)).decode()
    # Uses sudo where it exists; root-only servers (Debian's default) have no sudo and run sh directly.
    return f"echo {encoded} | base64 -d | gunzip | $(command -v sudo) sh"


def render_frpc(settings: Settings) -> str:
    if settings.mode == "hosted":
        return render_hosted_frpc(settings)
    host, port = RELAY_LISTENER.split(":")
    admin_host, admin_port = ADMIN.split(":")
    return f"""serverAddr = "{settings.relay_host}"
serverPort = {settings.relay_control_port}
auth.method = "token"
auth.token = "{settings.relay_token}"
transport.tls.enable = true
transport.tls.trustedCaFile = "{(PKI_DIR / "ca.crt").as_posix()}"
transport.tls.serverName = "{SERVER_NAME}"
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


def render_hosted_frpc(settings: Settings) -> str:
    """The shared relay routes by name: an HTTPS proxy for this Home Assistant's own hostname.

    The device id and key identify this Home Assistant to the relay's controller,
    which only lets it publish its own hostname.
    """
    host, port = RELAY_LISTENER.split(":")
    admin_host, admin_port = ADMIN.split(":")
    return f"""serverAddr = "{hosted.RELAY_HOST}"
serverPort = {hosted.RELAY_PORT}
user = "{settings.hosted_device}"
metadatas.key = "{settings.hosted_key}"
auth.method = "token"
auth.token = "{hosted.AUTH_TOKEN}"
transport.tls.enable = true
transport.tls.trustedCaFile = "{hosted.CA_FILE.as_posix()}"
transport.tls.serverName = "{hosted.SERVER_NAME}"
# The relay checks the membership and the speed limit at every heartbeat.
transport.heartbeatInterval = 30
transport.heartbeatTimeout = 90
loginFailExit = false
log.to = "console"
log.level = "warn"
webServer.addr = "{admin_host}"
webServer.port = {admin_port}

[[proxies]]
name = "airbound"
type = "https"
customDomains = ["{settings.fqdn}"]
localIP = "{host}"
localPort = {port}
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
        ensure_pki()
        FRPC_CONFIG.write_text(render_frpc(settings))
        os.chmod(FRPC_CONFIG, 0o600)
        self._process = await asyncio.create_subprocess_exec("frpc", "-c", str(FRPC_CONFIG))
        _LOGGER.info("Relay client started for %s", hosted.RELAY_HOST if settings.mode == "hosted" else settings.relay_host)

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
        for proxy in data.get("tcp", []) + data.get("https", []):
            if proxy.get("name") == "airbound":
                if proxy.get("status") == "running":
                    return {"state": "connected", "error": ""}
                return {"state": "connecting", "error": proxy.get("err", "")}
        return {"state": "connecting", "error": ""}


def explain(error: str) -> str:
    """frp errors in words a person can act on."""
    lowered = error.lower()
    if "membership ended" in lowered:
        return "Your AirBound membership has ended. Renew it on Patreon to use the hosted relay again."
    if "unknown device" in lowered:
        return "The hosted relay doesn't know this Home Assistant any more. Link it again in the first steps."
    if "token" in lowered or "authorization" in lowered:
        return "The relay rejected AirBound's key. Run the install command on the server again."
    if "port" in lowered and ("used" in lowered or "not allowed" in lowered):
        return "The relay can't use the public port. Something else on the server may use it."
    if error:
        return f"The relay reported: {error}"
    return ""
