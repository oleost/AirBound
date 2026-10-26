"""Home Assistant's HTTP server settings that AirBound needs.

Since HA 2026.8 these are stored settings, changed with http/config/configure.
HA applies a change as a trial after restarting and reverts it after 5 minutes
unless it is confirmed, so a wrong setting can't lock anyone out.
"""

from __future__ import annotations

from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from .supervisor import Supervisor

WANTED: dict[str, Any] = {
    "use_x_forwarded_for": True,
    "trusted_proxies": ["172.30.33.0/24"],
    "ip_ban_enabled": True,
    "login_attempts_threshold": 10,
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


async def apply(supervisor: Supervisor) -> None:
    (state,) = await supervisor.ws({"type": "http/config"})
    await supervisor.ws({"type": "http/config/configure", "config": merged(state["stable"])})


async def confirm(supervisor: Supervisor) -> None:
    await supervisor.ws({"type": "http/config/promote"})
