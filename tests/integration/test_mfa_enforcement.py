"""People without MFA lose remote access even with an app that logged in at home.

Runs through the live hosted address, where Home Assistant sees a public client
address (from the home network, HA treats every address as local).
"""

from __future__ import annotations

import asyncio
import secrets

import aiohttp
import pytest

from conftest import LOCAL_URL, LoginFlow, create_user, enable_totp
from ha import HA

WAIT = 90  # AirBound checks every minute


async def outside_status(session: aiohttp.ClientSession, url: str, tokens: dict) -> int:
    async with session.get(f"{url}/api/", headers={"Authorization": f"Bearer {tokens['access_token']}"}) as resp:
        return resp.status


async def local_only(user_id: str) -> bool:
    async with HA() as ha:
        return bool(next(u for u in await ha.call({"type": "config/auth/list"}) if u["id"] == user_id).get("local_only"))


async def wait_for(predicate, seconds: int = WAIT) -> bool:
    for _ in range(seconds // 5):
        if await predicate():
            return True
        await asyncio.sleep(5)
    return await predicate()


async def test_people_without_mfa_are_limited_until_they_turn_it_on(panel):
    """Watches the user's flag instead of polling from outside: every refused
    request counts towards Home Assistant's permanent IP ban."""
    settings = (await panel.get("api/state"))["settings"]
    if not (settings["live"] and settings["mode"] == "hosted"):
        pytest.skip("needs the hosted relay live on the test VM")
    url = settings["url"]
    name, password = "mfaprobe" + secrets.token_hex(3), secrets.token_urlsafe(16)
    async with HA() as ha:
        user_id = await create_user(ha, name, password)
    try:
        async with aiohttp.ClientSession() as home, aiohttp.ClientSession() as outside:
            flow = LoginFlow(home, LOCAL_URL)
            tokens = await flow.token((await flow.login(name, password))["result"])

            async def limited() -> bool:
                return await local_only(user_id)

            assert await wait_for(limited), "AirBound didn't limit the user"
            people = (await panel.get("api/users"))["users"]
            assert next(p for p in people if p["id"] == user_id)["auto_limited"]
            assert await outside_status(outside, url, tokens) == 401  # the token from home is refused outside

            # Turning MFA on lets them out again; the same token works outside.
            await enable_totp(home, name, password)

            async def released() -> bool:
                return not await local_only(user_id)

            assert await wait_for(released), "still limited after turning on MFA"
            assert await outside_status(outside, url, tokens) == 200
    finally:
        async with HA() as ha:
            await ha.call({"type": "config/auth/delete", "user_id": user_id})
