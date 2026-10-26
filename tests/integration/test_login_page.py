"""What a person actually sees on HA's login page when reaching it through AirBound."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from conftest import EXTERNAL_URL, FQDN, VM_HOST

SCREENSHOTS = Path(os.environ.get("AIRBOUND_SCREENSHOTS", Path(__file__).resolve().parents[2] / "test-results"))


@pytest.fixture
async def page(live):
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=[f"--host-resolver-rules=MAP {FQDN} {VM_HOST}"])
        page = await browser.new_page(ignore_https_errors=True, viewport={"width": 420, "height": 800})
        yield page
        await browser.close()


async def log_in(page, username: str, password: str) -> None:
    await page.goto(f"{EXTERNAL_URL}/", wait_until="networkidle")
    await page.locator("input[name=username]").fill(username)
    await page.locator("input#password").fill(password)
    await page.keyboard.press("Enter")


async def test_user_without_mfa_sees_what_to_do(page, users):
    await log_in(page, users["nomfa"]["username"], users["nomfa"]["password"])
    alert = page.locator("ha-alert[alert-type=error]")
    await alert.wait_for(timeout=10_000)
    SCREENSHOTS.mkdir(exist_ok=True)
    await page.screenshot(path=SCREENSHOTS / "login_refused_without_mfa.png")
    text = await alert.inner_text()
    assert "two-step verification (MFA)" in text
    assert "Security" in text


async def test_user_with_mfa_is_asked_for_code(page, users):
    await log_in(page, users["mfa"]["username"], users["mfa"]["password"])
    code_field = page.locator("input[name=code]")
    await code_field.wait_for(timeout=10_000)
