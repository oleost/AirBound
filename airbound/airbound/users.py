"""Home Assistant users and whether they have two-step login (MFA)."""

from __future__ import annotations

import json
from pathlib import Path

from .settings import Store
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


def mfa_plan(people: list[dict], limited: set[str], enforce: bool) -> tuple[list[dict], list[dict]]:
    """(limit, release): whom AirBound limits to the home network now, and whom it lets out again.

    The login guard stops new logins without MFA from outside, but an app that
    logged in at home keeps a token that works from anywhere. Home Assistant's
    own "local only" flag is checked on every request, so it stops those too.
    AirBound only ever releases the people it limited itself.
    """
    if not enforce:
        return [], [p for p in people if p["id"] in limited and p["local_only"]]
    limit = [p for p in people if not p["mfa"] and not p["local_only"]]
    release = [p for p in people if p["id"] in limited and p["mfa"] and p["local_only"]]
    return limit, release


async def enforce_mfa(supervisor: Supervisor, store: Store) -> tuple[list[str], list[str]]:
    """Applies mfa_plan; returns the names of the people limited and released just now."""
    settings = store.settings
    people = await list_people(supervisor, None)
    limited = set(settings.auto_limited)
    limit, release = mfa_plan(people, limited, settings.live and settings.require_mfa)
    for person in limit:
        await set_local_only(supervisor, person["id"], True)
        limited.add(person["id"])
    for person in release:
        await set_local_only(supervisor, person["id"], False)
        limited.discard(person["id"])
    limited &= {p["id"] for p in people if p["local_only"] or p in limit}  # forget deleted users and manual changes
    if limited != set(settings.auto_limited):
        store.update(auto_limited=tuple(sorted(limited)))
    return [p["name"] for p in limit], [p["name"] for p in release]
