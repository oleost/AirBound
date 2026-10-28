"""A Home Assistant backup of AirBound, restored: remote access waits for the person to confirm.

Runs where AirBound is live with a self-signed certificate (the beta VM). On a
Home Assistant with a real setup the live fixture skips it: a restore makes a
new machine key, so the hosted relay would want a Patreon login afterwards.
"""

from __future__ import annotations

import asyncio
import io
import os
import tarfile
from pathlib import Path

import aiohttp

from conftest import EXTERNAL_URL, HA, LOCAL_URL, VM_HOST, Panel, addon_slug, ingress

SCREENSHOT = Path(__file__).resolve().parents[2] / "test-results" / "restored-page.png"  # gitignored: shows real addresses


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


async def backup_files(backup: str, addon: str) -> list[str]:
    """The paths in the add-on's part of a backup, as Home Assistant's own download gives it."""
    headers = {"Authorization": f"Bearer {os.environ['HA_TOKEN']}"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(f"{LOCAL_URL}/api/hassio/backups/{backup}/download") as resp:
            assert resp.status == 200, f"backup download: {resp.status}"
            outer = await resp.read()
    with tarfile.open(fileobj=io.BytesIO(outer)) as backup_tar:
        member = next(m for m in backup_tar.getmembers() if m.name.endswith(f"{addon}.tar.gz"))
        inner = backup_tar.extractfile(member).read()
    with tarfile.open(fileobj=io.BytesIO(inner)) as addon_tar:
        return addon_tar.getnames()


async def notifications() -> list[dict]:
    async with HA() as ha:
        found = await ha.call({"type": "persistent_notification/get"})
    return list(found.values()) if isinstance(found, dict) else found


async def screenshot_panel() -> str:
    """The panel as the person sees it in Home Assistant, opened from a link to the overview."""
    from playwright.async_api import async_playwright

    base, cookie = await ingress()
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        await context.add_cookies([{"name": "ingress_session", "value": cookie, "domain": VM_HOST, "path": "/"}])
        page = await context.new_page()
        await page.goto(f"{base}#overview", wait_until="networkidle")  # an old link must not skip the question
        await page.get_by_role("heading", name="AirBound was restored from a backup").wait_for(timeout=15000)
        SCREENSHOT.parent.mkdir(exist_ok=True)
        await page.screenshot(path=str(SCREENSHOT), full_page=True)
        text = await page.inner_text("body")
        await browser.close()
    return text


async def test_a_restored_backup_stays_off_until_confirmed(live, external):
    assert await reachable(external)  # also writes to the access log, which the backup must leave out
    async with HA() as ha:
        slug = await addon_slug(ha)
        backup = (await ha.supervisor("/backups/new/partial", "post", {"name": "AirBound restore test", "addons": [slug]}, timeout=900))["slug"]
    try:
        files = await backup_files(backup, slug)
        assert any(f.endswith("data/settings.json") for f in files), files
        assert not [f for f in files if "data/logs" in f or f.endswith(("machine_key", "frpc.toml"))], "the backup holds the machine key or logs"

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

        text = await screenshot_panel()
        assert "Turn the old Home Assistant off first" in text and "Continue on this Home Assistant" in text

        async def notified() -> bool:
            return any("Restored from a backup" in (n.get("message") or "") for n in await notifications())

        assert await wait_for(notified, 90), "Home Assistant shows no notification about the restore"

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
