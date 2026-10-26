"""Apply AirBound's HTTP server settings on the test VM (what the wizard's "Fix it for me" will do).

Sends the settings as a trial with http/config/configure, waits for Home Assistant
to restart, then promotes them so HA doesn't revert after 5 minutes.
"""

from __future__ import annotations

import asyncio

from ha import HA

AIRBOUND = {
    "use_x_forwarded_for": True,
    "trusted_proxies": ["172.30.33.0/24"],
    "ip_ban_enabled": True,
    "login_attempts_threshold": 10,
}
META = {"created_at", "error", "error_message"}


async def wait_for_ha(timeout: int = 300) -> HA:
    for _ in range(timeout // 5):
        await asyncio.sleep(5)
        try:
            ha = await HA().__aenter__()
            if (await ha.call({"type": "get_config"}))["state"] == "RUNNING":
                return ha
            await ha.__aexit__()
        except Exception:
            pass
    raise TimeoutError("Home Assistant did not come back")


async def main() -> None:
    async with HA() as ha:
        current = await ha.call({"type": "http/config"})
        stable = {k: v for k, v in current["stable"].items() if k not in META}
        wanted = {**stable, **AIRBOUND}
        if wanted == stable:
            print("already configured")
            return
        print("configure:", await ha.call({"type": "http/config/configure", "config": wanted}))
    ha = await wait_for_ha()
    state = await ha.call({"type": "http/config"})
    print("active:", state["active_config_type"], "revert_at:", state["revert_at"])
    await ha.call({"type": "http/config/promote"})
    print("promoted:", {k: (await ha.call({"type": "http/config"}))["stable"].get(k) for k in AIRBOUND})
    await ha.__aexit__()


if __name__ == "__main__":
    asyncio.run(main())
