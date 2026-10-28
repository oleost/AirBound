"""Home Assistant's HTTP server settings that AirBound needs.

Since HA 2026.8 these are stored settings, changed with http/config/configure.
HA applies a change as a trial after restarting and reverts it after 5 minutes
unless it is confirmed, so a wrong setting can't lock anyone out.
"""

from __future__ import annotations

from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from .settings import Store
from .supervisor import Supervisor

WANTED: dict[str, Any] = {
    "use_x_forwarded_for": True,
    "trusted_proxies": ["172.30.33.0/24"],
    "ip_ban_enabled": True,
    # HA's bans are permanent and its counter only resets on a successful login or a
    # restart. One Companion app with a login that no longer works makes about 6 refused
    # requests at once (tested), so 10 could shut a whole household out. CrowdSec blocks
    # password guessing first, and MFA makes a password alone useless.
    "login_attempts_threshold": 50,
}
LABELS = {
    "use_x_forwarded_for": "Trust the address AirBound passes on",
    "trusted_proxies": "Trusted proxy",
    "ip_ban_enabled": "Block addresses after failed logins",
    "login_attempts_threshold": "Failed logins before blocking",
}
_META = {"created_at", "error", "error_message"}
TEST_ADDRESS = "203.0.113.10"  # documentation range (RFC 5737), never a real client


def _show(value: Any) -> str:
    if value is None or value == -1:
        return "Not set" if value is None else "Never"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return ", ".join(value) or "Not set"
    return str(value)


def changes(config: dict) -> list[dict[str, str]]:
    """What has to change, in words the settings dialog in HA also uses."""
    return [
        {"label": LABELS[key], "current": _show(config.get(key)), "new": _show(value)}
        for key, value in WANTED.items()
        if config.get(key) != value
    ]


def merged(config: dict) -> dict:
    return {**{k: v for k, v in config.items() if k not in _META}, **WANTED}


async def proxy_trusted(session: ClientSession, ha_upstream: str) -> bool:
    """Does HA accept X-Forwarded-For from AirBound?

    HA answers 400 when an untrusted proxy sends the header. The manifest needs
    no login, so the check doesn't count as a failed login.
    """
    try:
        async with session.get(
            f"http://{ha_upstream}/manifest.json",
            headers={"X-Forwarded-For": TEST_ADDRESS},
            timeout=ClientTimeout(total=5),
        ) as resp:
            return resp.status == 200
    except (ClientError, TimeoutError):
        return False


async def status(supervisor: Supervisor, session: ClientSession, ha_upstream: str) -> dict:
    (state,) = await supervisor.ws({"type": "http/config"})
    stable = state["stable"]
    pending = state.get("pending")
    return {
        "changes": changes(stable),
        "pending": bool(pending) and not pending.get("error"),
        "pending_error": (pending or {}).get("error_message"),
        "revert_at": state.get("revert_at"),
        "proxy_trusted": await proxy_trusted(session, ha_upstream),
    }


def before(config: dict, recorded: dict) -> dict:
    """The values to put back on removal: what each setting was before AirBound first changed it
    (None: not set). A value recorded earlier wins, since by now it may be AirBound's own."""
    return {**{k: config.get(k) for k in WANTED}, **recorded}


def restored(config: dict, recorded: dict) -> dict | None:
    """The settings with AirBound's values put back as they were, or None if there's nothing to do.

    Only settings that still have AirBound's value change: one the person has
    changed since is theirs now. A setting that wasn't there goes away again.
    """
    back = {k: v for k, v in recorded.items() if k in WANTED and config.get(k) == WANTED[k] and v != WANTED[k]}
    if not back:
        return None
    result = {k: v for k, v in config.items() if k not in _META}
    for key, value in back.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = value
    return result


def unknown(config: dict, recorded: dict) -> list[dict[str, str]]:
    """AirBound's settings whose earlier value wasn't recorded (set up before AirBound kept it)."""
    return [{"label": LABELS[k], "current": _show(config.get(k))} for k in WANTED if k not in recorded and config.get(k) == WANTED[k]]


async def apply(supervisor: Supervisor, store: Store | None = None) -> None:
    (state,) = await supervisor.ws({"type": "http/config"})
    if store is not None:
        store.update(ha_http_before=before(state["stable"], store.settings.ha_http_before))
    await supervisor.ws({"type": "http/config/configure", "config": merged(state["stable"])})


async def confirm(supervisor: Supervisor) -> None:
    await supervisor.ws({"type": "http/config/promote"})
