"""Which setup steps are done, which can be opened, and a one-line summary of each.

Steps unlock by what they depend on, not strictly in order: the domain, the
connection method, Home Assistant's settings and the users can be done in any
order, going live needs all of them, and the router and phone steps need a
live address to test against.
"""

from __future__ import annotations

import ipaddress
from dataclasses import asdict, dataclass

from .settings import Settings

TITLES = {
    "cloudflare": "Your domain",
    "connection": "How to connect",
    "relay": "Relay server",
    "ha_settings": "Home Assistant settings",
    "users": "Two-step login",
    "go_live": "Go live",
    "router": "Your router",
    "devices": "Your phone",
}


@dataclass
class Step:
    id: str
    title: str
    done: bool
    locked: bool
    reason: str  # why it's locked, in words
    summary: str  # what's set up, in words


def _is_public(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return bool(host)  # a hostname; checked when going live


def compute(settings: Settings, http_ok: bool, blocking: list[str], people: int) -> list[Step]:
    s = settings
    relay = s.mode == "relay"
    done = {
        "cloudflare": bool(s.cloudflare_token and s.fqdn and s.tls == "cloudflare"),
        "connection": s.mode in ("direct", "relay"),
        "relay": relay and bool(s.relay_host) and _is_public(s.relay_host),
        "ha_settings": http_ok,
        "users": not blocking,
        "go_live": s.live,
        "router": s.router_done or bool(s.port_mapping),
        "devices": s.devices_done,
    }
    needs = {
        "relay": ["connection"],
        "go_live": ["cloudflare", "connection", "ha_settings", "users"] + (["relay"] if relay else []),
        "router": ["go_live"],
        "devices": ["go_live"],
    }
    summaries = {
        "cloudflare": s.fqdn if done["cloudflare"] else "",
        "connection": {"relay": "Through your relay server", "direct": "Through your router"}.get(s.mode, ""),
        "relay": s.relay_host if done["relay"] else "",
        "ha_settings": "Sees visitors' addresses, blocks repeated failed logins" if http_ok else "",
        "users": (f"{people} {'person' if people == 1 else 'people'}, all set" if not blocking else ""),
        "go_live": s.url if s.live else "",
        "router": (f"TCP port {s.external_port} forwarded" if done["router"] else ""),
        "devices": "Address ready for the app and browsers" if done["devices"] else "",
    }
    order = [x for x in TITLES if not (x == "relay" and not relay) and not (x == "router" and relay)]
    steps = []
    for step_id in order:
        missing = [TITLES[n] for n in needs.get(step_id, []) if not done[n]]
        if step_id == "relay" and not relay:
            missing = [TITLES["connection"]]
        steps.append(
            Step(
                id=step_id,
                title=TITLES[step_id],
                done=done[step_id],
                locked=bool(missing),
                reason=f"Needs: {', '.join(missing)}" if missing else "",
                summary=summaries[step_id],
            )
        )
    return steps


def as_dicts(steps: list[Step]) -> list[dict]:
    return [asdict(step) for step in steps]


def complete(steps: list[Step]) -> bool:
    return all(step.done for step in steps)


def landing(settings: Settings, steps: list[Step]) -> str:
    """Where the panel opens: the overview once set up, else the first step that needs attention."""
    if settings.live and complete(steps):
        return "overview"
    if not settings.cloudflare_token and not settings.mode and not settings.live:
        return "welcome"
    for step in steps:
        if not step.done and not step.locked:
            return step.id
    return "overview" if settings.live else steps[0].id
