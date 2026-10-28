"""AirBound's settings, managed from the web UI and stored in /data/settings.json."""

from __future__ import annotations

import functools
import json
import os
import re
import secrets
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

SETTINGS_FILE = Path("/data/settings.json")
ALL_STEPS = ("welcome", "connection", "cloudflare", "hosted", "relay", "ha_settings", "users", "go_live", "router", "devices", "overview", "done")
MODE_STEPS = {  # steps only some connection modes have
    "cloudflare": ("direct", "relay"),
    "hosted": ("hosted",),
    "relay": ("relay",),
    "router": ("direct",),
}


def wizard_steps(mode: str) -> list[str]:
    """The wizard's steps for a connection mode: only direct mode has a router step, and so on."""
    return [s for s in ALL_STEPS if s not in MODE_STEPS or mode in MODE_STEPS[s]]


HOSTNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
# Names people try first when looking for a Home Assistant. Allowed, but the UI warns.
COMMON_HOSTNAMES = frozenset(
    {"ha", "hass", "home", "homeassistant", "home-assistant", "smarthome", "smart", "hjem", "huset", "house", "iot", "hub", "remote", "vpn", "www"}
)


def valid_hostname(name: str) -> bool:
    return bool(HOSTNAME_RE.match(name))


WORDS_FILE = Path(__file__).with_name("words.txt")


@functools.cache
def address_words() -> tuple[str, ...]:
    """The hand-picked words in words.txt (the relay server keeps an identical copy)."""
    lines = WORDS_FILE.read_text(encoding="utf-8").splitlines()
    return tuple(line.strip() for line in lines if line.strip() and not line.startswith("#"))


def random_hostname() -> str:
    """Three different random words, such as otter-comet-maple: easy to say and type,
    and with about 960 million combinations still impractical to guess."""
    return "-".join(secrets.SystemRandom().sample(address_words(), 3))


@dataclass(frozen=True)
class Settings:
    domain: str = ""
    zone_id: str = ""
    hostname: str = ""
    cloudflare_token: str = ""
    tls: str = "cloudflare"  # "hosted": certificate through the hosted relay; "internal": self-signed, for tests
    require_mfa: bool = True
    wizard_step: str = "welcome"
    live: bool = False
    public_ip: str = ""
    external_port: int = 443  # the public port: on the router (direct) or on the relay
    mode: str = ""  # "direct" through the router, "relay" through the user's own server, "hosted" through AirBound's
    relay_host: str = ""
    relay_token: str = ""
    relay_control_port: int = 7000
    port_mapping: str = ""  # "natpmp" or "upnp" when AirBound opened the router port itself
    router_done: bool = False  # the user confirmed the port forward works
    devices_done: bool = False  # the user has seen how to connect their phone
    install_id: str = ""  # identifies this Home Assistant to the hosted relay, so relinking keeps the address
    hosted_device: str = ""
    hosted_key: str = ""  # the hosted relay's key for this Home Assistant
    hosted_claim: str = ""  # a link request waiting for the Patreon login
    hosted_poll: str = ""
    hosted_code: str = ""
    hosted_since: str = ""  # when this hostname was first linked (ISO 8601 UTC), for the certificate watch
    auto_limited: tuple[str, ...] = ()  # users AirBound limited to the home network because they have no MFA
    ha_http_before: dict = field(default_factory=dict)  # HA's HTTP settings before AirBound changed them, to put back
    paused: bool = False  # the person turned remote access off; going live turns it on again
    removed: bool = False  # the person removed AirBound's changes; only uninstalling is left
    removed_notes: tuple[str, ...] = ()  # what the person still has to do by hand after removing
    machine_known: bool = False  # this setup has a machine key (machine.py), so a restored backup can be told apart
    restored: bool = False  # came back from a backup; remote access waits until the person confirms

    @property
    def fqdn(self) -> str:
        """The full external hostname, e.g. k7f3q9x2m4p8.example.com."""
        return f"{self.hostname}.{self.domain}" if self.hostname and self.domain else ""

    @property
    def url(self) -> str:
        if not self.fqdn:
            return ""
        return f"https://{self.fqdn}" + ("" if self.external_port == 443 else f":{self.external_port}")

    @property
    def is_configured(self) -> bool:
        if not self.fqdn:
            return False
        if self.tls == "hosted":
            return bool(self.hosted_key)
        return self.tls == "internal" or bool(self.cloudflare_token)

    def public(self) -> dict:
        """What the web UI may see. Secrets are never sent back."""
        data = asdict(self)
        for secret in ("cloudflare_token", "relay_token", "hosted_key", "hosted_poll"):
            data.pop(secret)
        data["has_token"] = bool(self.cloudflare_token)
        data["has_hosted_key"] = bool(self.hosted_key)
        data["fqdn"] = self.fqdn
        data["url"] = self.url
        return data


class Store:
    def __init__(self, path: Path = SETTINGS_FILE) -> None:
        self._path = path
        self.settings = self._load()

    def _load(self) -> Settings:
        if not self._path.exists():
            return Settings()
        raw = json.loads(self._path.read_text())
        known = {f.name for f in fields(Settings)}
        return Settings(**{k: v for k, v in raw.items() if k in known})

    def update(self, **changes) -> Settings:
        return self._save(replace(self.settings, **changes))

    def reset(self, **keep) -> Settings:
        """Back to a fresh install, with only the given values."""
        return self._save(Settings(**keep))

    def _save(self, settings: Settings) -> Settings:
        self.settings = settings
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.settings), indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self._path)
        return self.settings
