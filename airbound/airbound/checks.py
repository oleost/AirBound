"""Health checks for the status page. Each says what is wrong and what to do about it."""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
import socket
import time
from dataclasses import asdict, dataclass

from aiohttp import ClientError, ClientSession, ClientTimeout

from . import ha_bans, hosted, network, relay, users
from .certwatch import CertWatch
from .cloudflare import Cloudflare, CloudflareError
from .crowdsec import CrowdSec
from .settings import Settings
from .supervisor import Supervisor

ADDON_NETWORK = ipaddress.ip_network("172.30.33.0/24")
OUTSIDE_TEST_ADDRESS = "198.51.100.23"  # documentation range (RFC 5737): stands in for any outsider


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


def check_password_login(providers: list[dict] | None) -> Check:
    """Login methods offered to an outside address; anything but a password one skips password and MFA."""
    title = "Password for every login"
    if providers is None:
        return Check("trusted_networks", title, True, "Couldn't ask Home Assistant right now; will check again.")
    others = sorted({p.get("type", "") for p in providers if p.get("type") != "homeassistant"})
    if not others:
        return Check("trusted_networks", title, True, "Every login from outside needs a password.")
    return Check(
        "trusted_networks",
        title,
        False,
        f"Home Assistant offers outside visitors a login without password ({', '.join(others)}). "
        "That skips both password and MFA. Remove it (usually trusted_networks in configuration.yaml) "
        "and restart Home Assistant.",
    )


async def outside_login_providers(session: ClientSession, ha_upstream: str) -> list[dict] | None:
    """The login methods Home Assistant shows to a visitor from the internet.

    HA lists only the providers the caller may use, and trusts X-Forwarded-For
    from AirBound, so asking with an outside test address shows exactly what an
    outsider would get, wherever the configuration lives.
    """
    try:
        async with session.get(
            f"http://{ha_upstream}/auth/providers",
            headers={"X-Forwarded-For": OUTSIDE_TEST_ADDRESS},
            timeout=ClientTimeout(total=5),
        ) as resp:
            if resp.status != 200:
                return None
            return (await resp.json()).get("providers", [])
    except (ClientError, TimeoutError, ValueError):
        return None


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


async def resolve_all(name: str) -> set[str]:
    """The IPv4 addresses a name points to, without blocking the event loop."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(name, 443, family=socket.AF_INET)
    except OSError:
        return set()
    return {info[4][0] for info in infos}


async def check_dns(settings: Settings, session: ClientSession) -> Check:
    if settings.mode == "hosted":
        current, resolved = await asyncio.gather(relay.resolve_ipv4(hosted.RELAY_HOST), resolve_all(settings.fqdn))
        if current and current in resolved:
            return Check("dns", "Address", True, f"{settings.fqdn} points to AirBound's hosted relay.")
        return Check("dns", "Address", False, f"{settings.fqdn} doesn't point to AirBound's hosted relay right now. This is fixed on the relay's side; try again later.")
    relay_mode = settings.mode == "relay"
    current, resolved = await asyncio.gather(
        relay.resolve_ipv4(settings.relay_host) if relay_mode else network.public_ip(session),
        resolve_all(settings.fqdn),
    )
    target = "your relay server" if relay_mode else "your home"
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
    if settings.mode == "hosted":
        if status["state"] == "connected":
            return Check("relay", "Hosted relay", True, "Connected to AirBound's hosted relay. Your router needs no open ports.")
        detail = relay.explain(status["error"]) or (
            "AirBound can't reach the hosted relay right now. It keeps trying by itself; check that Home Assistant has internet access."
        )
        return Check("relay", "Hosted relay", False, detail)
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
    permanent = len(ha_bans.read_bans())
    if permanent:
        blocked += f" Home Assistant has blocked {permanent} address{'es' if permanent != 1 else ''} for good; see Blocked addresses."
    return Check("protection", "Attack protection", True, f"CrowdSec is watching for attacks. {blocked}")


LOCK_CACHE = 600  # the CAA record only changes when going live; no need to ask Cloudflare on every check
_lock_cache: dict[tuple[str, str], tuple[float, bool]] = {}


async def certificates_locked(settings: Settings, session: ClientSession) -> bool | None:
    """Whether the CAA record is in place; None when Cloudflare can't be asked."""
    key = (settings.zone_id, settings.fqdn)
    cached = _lock_cache.get(key)
    if cached and time.monotonic() - cached[0] < LOCK_CACHE:
        return cached[1]
    try:
        locked = await Cloudflare(session, settings.cloudflare_token).certificates_locked(settings.zone_id, settings.fqdn)
    except CloudflareError:
        return None
    _lock_cache.clear()
    if locked:  # a missing record is asked about again, so fixing it shows right away
        _lock_cache[key] = (time.monotonic(), locked)
    return locked


async def check_certificate_lock(settings: Settings, session: ClientSession, certwatch: CertWatch | None) -> Check:
    """Nobody but AirBound may hold a certificate for the hostname."""
    title = "Certificate lock"
    checked, unexpected = certwatch.results_for(settings.fqdn) if certwatch else (False, [])
    if unexpected:
        issuers = ", ".join(sorted({c["issuer"].split("CN=")[-1] for c in unexpected}))
        return Check(
            "cert_lock",
            title,
            False,
            f"A certificate for {settings.fqdn} was issued by {issuers}, and AirBound didn't order it. "
            "Someone may be trying to intercept your traffic. Turn off remote access until you know why.",
        )
    if settings.tls == "hosted":
        if checked:
            return Check("cert_lock", title, True, "Certificate logs show no certificate for this address except the ones this Home Assistant made.")
        return Check("cert_lock", title, True, "AirBound watches the public certificate logs for this address.")
    locked = await certificates_locked(settings, session)
    if locked is False:
        return Check(
            "cert_lock",
            title,
            False,
            f"The DNS record that stops others from getting a certificate for {settings.fqdn} is missing.",
            "Go live again",
            "go_live",
        )
    watched = " Certificate logs show nothing unexpected." if checked else ""
    return Check("cert_lock", title, True, f"Only AirBound can get a certificate for this address.{watched}")


async def check_membership(settings: Settings, session: ClientSession) -> Check | None:
    """The hosted relay works while the Patreon membership is active."""
    title = "Membership"
    try:
        info = await hosted.account(session, settings.hosted_key)
    except hosted.HostedError:
        return None  # the website being unreachable says nothing about the membership
    state = info.get("state")
    if state == "active":
        return Check("membership", title, True, "Your AirBound membership is active.")
    if state == "grace":
        until = info.get("grace_until")
        when = f" until {datetime.datetime.fromtimestamp(until, datetime.UTC):%d %B}" if until else ""
        return Check(
            "membership",
            title,
            False,
            f"Patreon couldn't renew your membership, perhaps because of a card problem. Remote access keeps working{when}; "
            f"update your payment on Patreon: {info.get('manage_url', '')}",
        )
    if state == "expired":
        return Check(
            "membership",
            title,
            False,
            f"Your membership has ended, so the hosted relay is off. Renew it on Patreon: {info.get('join_url', '')}",
            "Link again",
            "hosted",
        )
    return Check("membership", title, False, "The hosted relay doesn't know this Home Assistant any more. Link it again.", "Link again", "hosted")


async def run_all(
    settings: Settings,
    supervisor: Supervisor,
    session: ClientSession,
    frpc: relay.Frpc,
    crowdsec: CrowdSec,
    user_id: str | None,
    certwatch: CertWatch | None = None,
) -> list[Check]:
    async def providers() -> list[dict] | None:
        return await outside_login_providers(session, await supervisor.ha_upstream())

    # Independent checks run side by side, so the slowest one sets the pace.
    (accounts, http), offered = await asyncio.gather(
        supervisor.ws({"type": "config/auth/list"}, {"type": "http/config"}),  # one connection for both
        providers(),
    )
    people = users.describe(accounts, users.enrolled_user_ids(), user_id)
    results = [
        check_mfa(people),
        check_http(http.get("stable") or {}),
        check_password_login(offered),
    ]
    if settings.live:
        pending = []
        if crowdsec:
            pending.append(check_protection(crowdsec))
        if settings.mode == "hosted":
            pending.append(check_membership(settings, session))
        if settings.mode in ("relay", "hosted"):
            pending.append(check_relay(settings, frpc, session))
        pending.append(check_certificate(settings))
        if settings.tls in ("cloudflare", "hosted"):
            pending.append(check_dns(settings, session))
            pending.append(check_certificate_lock(settings, session, certwatch))
        results += [c for c in await asyncio.gather(*pending) if c]
    return results
