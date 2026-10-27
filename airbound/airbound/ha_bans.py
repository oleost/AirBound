"""Home Assistant's own IP bans: permanent, stored in ip_bans.yaml in the config folder.

AirBound only reads them, to show them in the panel. HA has no API to lift a
ban, and AirBound deliberately has no write access to the config folder, so the
panel explains HA's documented way: remove the line, then restart.
"""

from __future__ import annotations

from pathlib import Path

from .users import HA_CONFIG

BANS_FILE = HA_CONFIG / "ip_bans.yaml"


def _key(line: str) -> str | None:
    """The address on a top-level line such as `203.0.113.5:` or `'2001:db8::1':`."""
    if not line or line[0].isspace() or line.lstrip().startswith("#"):
        return None
    stripped = line.rstrip()
    if not stripped.endswith(":"):
        return None
    return stripped[:-1].strip().strip("'\"")


def read_bans(path: Path = BANS_FILE) -> list[dict]:
    """[{"ip", "banned_at"}], newest last, as HA wrote them."""
    if not path.exists():
        return []
    bans: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        ip = _key(line)
        if ip:
            bans.append({"ip": ip, "banned_at": ""})
        elif bans and line.strip().startswith("banned_at:"):
            bans[-1]["banned_at"] = line.split(":", 1)[1].strip().strip("'\"")
    return bans
