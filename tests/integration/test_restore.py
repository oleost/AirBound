"""A Home Assistant backup of AirBound, restored: remote access waits for the person to confirm.

Runs where AirBound is live with a self-signed certificate (the beta VM). On a
Home Assistant with a real setup the live fixture skips it: a restore makes a
new machine key, so the hosted relay would want a Patreon login afterwards.
"""

from __future__ import annotations

import asyncio

import aiohttp

from conftest import EXTERNAL_URL, HA, Panel, addon_slug, ingress


async def reachable(external: aiohttp.ClientSession) -> bool:
    try:
        async with external.get(f"{EXTERNAL_URL}/manifest.json", timeout=aiohttp.ClientTimeout(total=5)) as resp:
            return resp.status == 200
    except (aiohttp.ClientError, TimeoutError):
        return False


async def panel_state() -> dict | None:
    try:
        base, cookie = await ingress()
        async with aiohttp.ClientSession(cookies={"ingress_session": cookie}) as session:
            return await Panel(session, base).get("api/state")
    except Exception:  # the add-on is still starting
        return None


async def wait_for(check, seconds: int = 60) -> bool:
    for _ in range(seconds // 2):
        if await check():
            return True
        await asyncio.sleep(2)
    return False


async def test_a_restored_backup_stays_off_until_confirmed(live, external):
    assert await reachable(external)
    async with HA() as ha:
        slug = await addon_slug(ha)
        backup = (await ha.supervisor("/backups/new/partial", "post", {"name": "AirBound restore test", "addons": [slug]}, timeout=900))["slug"]
    try:
        async with HA() as ha:
            await ha.supervisor(f"/backups/{backup}/restore/partial", "post", {"addons": [slug]}, timeout=900)

        state = None
        for _ in range(60):
            state = await panel_state()
            if state:
                break
            await asyncio.sleep(3)
        assert state, "the panel didn't come back after the restore"
        assert state["settings"]["restored"] and state["landing"] == "restored"
        assert state["settings"]["live"]  # the setup itself came back from the backup
        await asyncio.sleep(10)
        assert not await reachable(external), "a restored backup went live by itself"

        base, cookie = await ingress()
        async with aiohttp.ClientSession(cookies={"ingress_session": cookie}) as session:
            panel = Panel(session, base)
            await panel.post("api/go-live", expect=409)
            confirmed = await panel.post("api/restored")
        assert not confirmed["settings"]["restored"]
        assert await wait_for(lambda: reachable(external)), "remote access didn't start after confirming"
    finally:
        state = await panel_state()
        if state and state["settings"].get("restored"):  # leave the VM working for the next test
            base, cookie = await ingress()
            async with aiohttp.ClientSession(cookies={"ingress_session": cookie}) as session:
                await Panel(session, base).post("api/restored")
        async with HA() as ha:
            await ha.supervisor(f"/backups/{backup}", "delete")
