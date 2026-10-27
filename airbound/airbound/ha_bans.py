"""Home Assistant's own IP bans: permanent, stored in ip_bans.yaml in the config folder.

HA has no API to lift a ban. The documented way is to remove the address from
ip_bans.yaml and restart Home Assistant, which is what the panel does. This is
the only file AirBound writes in the config folder.
"""

from __future__ import annotations

import ipaddress
import os
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


def remove_ban(ip: str, path: Path = BANS_FILE) -> bool:
    """Drop one address from the file; True if it was there. HA applies it after a restart."""
    ipaddress.ip_address(ip)  # only ever a single, valid address
    if not path.exists():
        return False
    kept, skipping, found = [], False, False
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        key = _key(line)
        if key is not None:
            skipping = key == ip
            found = found or skipping
        if not skipping:
            kept.append(line)
    if found:
        tmp = path.with_name(path.name + ".airbound-tmp")
        tmp.write_text("".join(kept), encoding="utf-8")
        os.replace(tmp, path)
    return found
