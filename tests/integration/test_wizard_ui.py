"""Walks through every wizard screen in a real browser, on a phone and a desktop.

Screenshots land in test-results/ for review.
"""

from __future__ import annotations

import pytest
from playwright.async_api import async_playwright

from conftest import ingress
from test_login_page import SCREENSHOTS

EXPECTED = {
    "welcome": "Reach your Home Assistant from anywhere",
    "cloudflare": "Connect your domain",
    "connection": "How should your phone reach Home Assistant?",
    "relay": "Set up your relay server",
    "ha_settings": "Let Home Assistant see who is connecting",
    "users": "Two-step login for everyone who logs in from outside",
    "go_live": "Go live",
    "router": "Let the traffic in through your router",
    "devices": "Connect your phone",
    "overview": "Remote access",
}
VIEWPORTS = {"phone": {"width": 390, "height": 844}, "desktop": {"width": 1100, "height": 900}}


@pytest.fixture
async def browser():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        yield browser
        await browser.close()


@pytest.mark.parametrize("device", VIEWPORTS)
async def test_every_step_renders_cleanly(browser, panel, live, device):
    base, cookie = await ingress()
    context = await browser.new_context(viewport=VIEWPORTS[device])
    host = base.split("/")[2].split(":")[0]
    await context.add_cookies([{"name": "ingress_session", "value": cookie, "domain": host, "path": "/"}])
    page = await context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    SCREENSHOTS.mkdir(exist_ok=True)
    try:
        state = await panel.get("api/state")
        available = {"welcome", "overview"} | {s["id"] for s in state["steps"] if not s["locked"]}
        for i, (step, heading) in enumerate(EXPECTED.items()):
            if step not in available:
                continue
            await page.goto(f"{base}#{step}", wait_until="networkidle")
            await page.reload(wait_until="networkidle")
            await page.get_by_role("heading", level=1).filter(has_text=heading).wait_for(timeout=10_000)
            await page.wait_for_timeout(1500)  # let the step load its data
            await page.screenshot(path=SCREENSHOTS / f"wizard_{device}_{i}_{step}.png", full_page=True)
            overflow = await page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
            assert not overflow, f"{step} scrolls sideways on {device}"
    finally:
        await context.close()
    assert not errors, errors
