"""Home Assistant users and whether they have two-step login (MFA)."""

from __future__ import annotations

import json
from pathlib import Path

from .supervisor import Supervisor

HA_CONFIG = Path("/homeassistant")
# Each MFA module keeps the IDs of enrolled users in its own store. Only the
# user IDs are read; the secrets in these files are never used or logged.
MFA_STORES = ("auth_module.totp", "auth_module.notify")


def enrolled_user_ids(storage_dir: Path = HA_CONFIG / ".storage") -> set[str]:
    ids: set[str] = set()
    for name in MFA_STORES:
        path = storage_dir / name
        if path.exists():
            ids |= set(json.loads(path.read_text()).get("data", {}).get("users", {}))
    return ids


def describe(users: list[dict], enrolled: set[str], current_user_id: str | None) -> list[dict]:
    """People who can log in, with what AirBound needs to know about each."""
    return [
        {
            "id": u["id"],
            "name": u["name"],
            "mfa": u["id"] in enrolled,
            "local_only": bool(u.get("local_only")),
            "owner": bool(u.get("is_owner")),
            "you": u["id"] == current_user_id,
        }
        for u in users
        if u.get("is_active") and not u.get("system_generated") and u.get("credentials")
    ]


def blocking(people: list[dict]) -> list[str]:
    """Names of people who could log in remotely without MFA."""
    return [p["name"] for p in people if not p["mfa"] and not p["local_only"]]


async def list_people(supervisor: Supervisor, current_user_id: str | None) -> list[dict]:
    (users,) = await supervisor.ws({"type": "config/auth/list"})
    return describe(users, enrolled_user_ids(), current_user_id)


async def set_local_only(supervisor: Supervisor, user_id: str, local_only: bool) -> None:
    await supervisor.ws({"type": "config/auth/update", "user_id": user_id, "local_only": local_only})
