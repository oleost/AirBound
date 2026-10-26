"""Health checks for the status page. Each says what is wrong and what to do about it."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import asdict, dataclass
from pathlib import Path

from aiohttp import ClientSession

from . import network, relay, users
from .crowdsec import CrowdSec
from .settings import Settings
from .supervisor import Supervisor

HA_CONFIG = Path("/homeassistant")
ADDON_NETWORK = ipaddress.ip_network("172.30.33.0/24")


@dataclass
class Check:
    id: str
    title: str
    ok: bool
    detail: str
    fix_label: str | None = None
    fix_step: str | None = None  # wizard step that fixes it

    def as_dict(self) -> dict:
        return asdict(self)


def check_mfa(people: list[dict]) -> Check:
    missing = users.blocking(people)
    if not missing:
        return Check("mfa", "Two-step login", True, "Everyone who can log in from outside uses two-step login (MFA).")
    return Check(
        "mfa",
        "Two-step login",
        False,
        f"{', '.join(missing)} can't log in from outside yet. "
        "They need two-step login (MFA), or can be limited to the home network.",
        "Set up two-step login",
        "users",
    )


def check_http(config: dict) -> Check:
    """HA must trust only the add-on network as proxy, and block repeated failed logins."""
    proxies = [ipaddress.ip_network(p, strict=False) for p in config.get("trusted_proxies") or []]
    problems = []
    if not config.get("use_x_forwarded_for") or not any(p.overlaps(ADDON_NETWORK) for p in proxies):
        problems.append("Home Assistant doesn't trust AirBound yet, so it can't see who is connecting")
    if any(p.prefixlen < 16 for p in proxies):
        problems.append("the trusted proxy list is broader than needed")
    threshold = config.get("login_attempts_threshold", -1)
    if not config.get("ip_ban_enabled") or threshold is None or threshold < 1:
        problems.append("repeated failed logins are never blocked")
    if not problems:
        return Check("http", "Home Assistant settings", True, "Home Assistant sees who connects and blocks repeated failed logins.")
    detail = "; ".join(problems)
    return Check("http", "Home Assistant settings", False, detail[0].upper() + detail[1:] + ".", "Fix settings", "ha_settings")


def check_trusted_networks(configuration_yaml: str) -> Check:
    if "trusted_networks" not in configuration_yaml:
        return Check("trusted_networks", "Password for every login", True, "Every login needs a password.")
    return Check(
        "trusted_networks",
        "Password for every login",
        False,
        "Your configuration.yaml has a \"trusted_networks\" login, which lets people in without password or MFA. "
        "Remove it from configuration.yaml and restart Home Assistant.",
    )


async def check_certificate(settings: Settings) -> Check:
    ok = await network.tls_reachable("127.0.0.1", settings.fqdn, verify=settings.tls != "internal")
    if ok:
        return Check("certificate", "Secure connection", True, "The certificate is valid and the connection is encrypted.")
    return Check(
        "certificate",
        "Secure connection",
        False,
        "AirBound has no valid certificate right now. It retries by itself; if this stays red, run the go-live step again.",
        "Go live again",
        "go_live",
    )


async def check_dns(settings: Settings, session: ClientSession) -> Check:
    relay_mode = settings.mode == "relay"
    current = await relay.resolve_ipv4(settings.relay_host) if relay_mode else await network.public_ip(session)
    target = "your relay server" if relay_mode else "your home"
    try:
        resolved = {info[4][0] for info in socket.getaddrinfo(settings.fqdn, 443, socket.AF_INET)}
    except OSError:
        resolved = set()
    if current and current in resolved:
        return Check("dns", "Address", True, f"{settings.fqdn} points to {target} ({current}).")
    return Check(
        "dns",
        "Address",
        False,
        f"{settings.fqdn} doesn't point to {target} ({current or 'unknown'}) yet. "
        + ("Run the go-live step again." if relay_mode else "AirBound updates it every 5 minutes; changes can take a few minutes to spread."),
        "Go live again" if relay_mode else None,
        "go_live" if relay_mode else None,
    )


async def check_relay(settings: Settings, frpc: relay.Frpc, session: ClientSession) -> Check:
    status = await frpc.status(session)
    if status["state"] == "connected":
        return Check("relay", "Relay server", True, f"Connected to {settings.relay_host}. Your router needs no open ports.")
    detail = relay.explain(status["error"]) or (
        f"AirBound can't reach the relay at {settings.relay_host} right now. It keeps trying by itself. "
        "If this stays red, check that the server is running."
    )
    return Check("relay", "Relay server", False, detail, "Check relay setup", "relay")


async def check_protection(crowdsec: CrowdSec) -> Check:
    if not crowdsec.running:
        return Check(
            "protection",
            "Attack protection",
            False,
            "CrowdSec isn't running, so addresses that attack aren't blocked automatically. "
            "Home Assistant still blocks an address after 10 failed logins. Restart AirBound to try again.",
        )
    bans = await crowdsec.bans()
    blocked = f"{len(bans)} address{'es' if len(bans) != 1 else ''} blocked right now." if bans else "Nothing blocked right now."
    return Check("protection", "Attack protection", True, f"CrowdSec is watching for attacks. {blocked}")


async def run_all(
    settings: Settings,
    supervisor: Supervisor,
    session: ClientSession,
    frpc: relay.Frpc,
    crowdsec: CrowdSec,
    user_id: str | None,
) -> list[Check]:
    people = await users.list_people(supervisor, user_id)
    (http,) = await supervisor.ws({"type": "http/config"})
    config_yaml = HA_CONFIG / "configuration.yaml"
    results = [
        check_mfa(people),
        check_http(http.get("stable") or {}),
        check_trusted_networks(config_yaml.read_text() if config_yaml.exists() else ""),
    ]
    if settings.live:
        if crowdsec:
            results.append(await check_protection(crowdsec))
        if settings.mode == "relay":
            results.append(await check_relay(settings, frpc, session))
        results.append(await check_certificate(settings))
        if settings.tls == "cloudflare":
            results.append(await check_dns(settings, session))
    return results
