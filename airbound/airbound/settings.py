"""AirBound's settings, managed from the web UI and stored in /data/settings.json."""

from __future__ import annotations

import json
import os
import re
import secrets
import string
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

SETTINGS_FILE = Path("/data/settings.json")
ALL_STEPS = ("welcome", "cloudflare", "connection", "relay", "ha_settings", "users", "go_live", "router", "devices", "done")


def wizard_steps(mode: str) -> list[str]:
    """The wizard's steps for a connection mode: relay mode needs no router step."""
    skip = "router" if mode == "relay" else "relay"
    return [s for s in ALL_STEPS if s != skip]


HOSTNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
# Names people try first when looking for a Home Assistant. Allowed, but the UI warns.
COMMON_HOSTNAMES = frozenset(
    {"ha", "hass", "home", "homeassistant", "home-assistant", "smarthome", "smart", "hjem", "huset", "house", "iot", "hub", "remote", "vpn", "www"}
)


def valid_hostname(name: str) -> bool:
    return bool(HOSTNAME_RE.match(name))


def random_hostname() -> str:
    """A hostname nobody can guess, like Home Assistant Link's random URLs."""
    alphabet = string.ascii_lowercase + string.digits
    return secrets.choice(string.ascii_lowercase) + "".join(secrets.choice(alphabet) for _ in range(11))


@dataclass(frozen=True)
class Settings:
    domain: str = ""
    zone_id: str = ""
    hostname: str = ""
    cloudflare_token: str = ""
    tls: str = "cloudflare"  # "internal" is a self-signed certificate, for testing only
    require_mfa: bool = True
    wizard_step: str = "welcome"
    live: bool = False
    public_ip: str = ""
    external_port: int = 443  # the public port: on the router (direct) or on the relay
    mode: str = ""  # "direct" through the router, or "relay" through the user's own server
    relay_host: str = ""
    relay_token: str = ""
    relay_control_port: int = 7000
    port_mapping: str = ""  # "natpmp" or "upnp" when AirBound opened the router port itself

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
        return self.tls == "internal" or bool(self.cloudflare_token)

    def public(self) -> dict:
        """What the web UI may see. Secrets are never sent back."""
        data = asdict(self)
        data.pop("cloudflare_token")
        data.pop("relay_token")
        data["has_token"] = bool(self.cloudflare_token)
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
        self.settings = replace(self.settings, **changes)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.settings), indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self._path)
        return self.settings
