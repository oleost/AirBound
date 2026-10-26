"""Which setup steps are done, which can be opened, and a one-line summary of each.

The connection method comes first, because it decides which steps follow: the
hosted relay needs a Patreon login instead of a domain, a relay server needs
setting up, and only the direct route needs the router. After that, steps
unlock by what they depend on: going live needs everything before it, and the
router and phone steps need a live address to test against.
"""

from __future__ import annotations

import ipaddress
from dataclasses import asdict, dataclass

from .settings import MODE_STEPS, Settings

TITLES = {
    "connection": "How to connect",
    "cloudflare": "Your domain",
    "hosted": "Your AirBound address",
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
    hosted = s.mode == "hosted"
    done = {
        "connection": s.mode in ("direct", "relay", "hosted"),
        "cloudflare": bool(s.cloudflare_token and s.fqdn and s.tls == "cloudflare"),
        "hosted": hosted and bool(s.hosted_key and s.fqdn and s.tls == "hosted"),
        "relay": relay and bool(s.relay_host) and _is_public(s.relay_host),
        "ha_settings": http_ok,
        "users": not blocking,
        "go_live": s.live,
        "router": s.router_done or bool(s.port_mapping),
        "devices": s.devices_done,
    }
    needs = {
        "cloudflare": ["connection"],
        "hosted": ["connection"],
        "relay": ["connection"],
        "go_live": ["connection"]
        + [n for n in ("cloudflare", "hosted", "relay") if s.mode in MODE_STEPS[n]]
        + ["ha_settings", "users"],
        "router": ["go_live"],
        "devices": ["go_live"],
    }
    summaries = {
        "connection": {"relay": "Through your relay server", "direct": "Through your router", "hosted": "Through AirBound's hosted relay"}.get(s.mode, ""),
        "cloudflare": s.fqdn if done["cloudflare"] else "",
        "hosted": s.fqdn if done["hosted"] else "",
        "relay": s.relay_host if done["relay"] else "",
        "ha_settings": "Sees visitors' addresses, blocks repeated failed logins" if http_ok else "",
        "users": (f"{people} {'person' if people == 1 else 'people'}, all set" if not blocking else ""),
        "go_live": s.url if s.live else "",
        "router": (f"TCP port {s.external_port} forwarded" if done["router"] else ""),
        "devices": "Address ready for the app and browsers" if done["devices"] else "",
    }
    order = [x for x in TITLES if x not in MODE_STEPS or s.mode in MODE_STEPS[x]]
    steps = []
    for step_id in order:
        missing = [TITLES[n] for n in needs.get(step_id, []) if not done[n]]
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
