"""Screenshots of the panel for the README, taken from the test system.

Read-only: it only navigates. Before each screenshot the real address, people's
names and IP addresses on the page are swapped for examples, so nothing about the
test system ends up in the repository.

    python scripts/readme_screenshots.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "integration"))
import aiohttp  # noqa: E402
from conftest import VM_HOST, ingress  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

OUT = ROOT / "docs" / "screenshots"
EXAMPLE_HOST = "k7f3q9x2m4p8"
EXAMPLE_NAMES = ["Alex", "Sam", "Robin", "Kim", "Charlie"]

MASK = """([fqdn, host, names, example, exampleNames]) => {
  const swaps = [[fqdn, example + ".airbound.dev"], [host, example]];
  names.forEach((n, i) => n && swaps.push([n, exampleNames[i % exampleNames.length]]));
  let ip = 10;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    let text = node.nodeValue;
    for (const [from, to] of swaps) if (from) text = text.split(from).join(to);
    text = text.replace(/\\b\\d{1,3}(\\.\\d{1,3}){3}\\b/g, () => "203.0.113." + ip++);
    node.nodeValue = text;
  }
  document.querySelectorAll("[data-text]").forEach((el) => (el.dataset.text = ""));
  document.querySelectorAll("img[src$='qr.svg']").forEach((el) => el.closest(".qr")?.remove());
}"""


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    base, cookie = await ingress()
    async with aiohttp.ClientSession(cookies={"ingress_session": cookie}) as s:
        async with s.get(base + "api/state") as r:
            state = await r.json()
        async with s.get(base + "api/users") as r:
            people = [u["name"] for u in (await r.json())["users"]]
        async with s.post("https://airbound.dev/api/v1/claims", json={"install_id": "readme-screenshot-0001"}) as r:
            claim = await r.json()  # an unused link request, only to show the code page
    fqdn = state["settings"]["fqdn"]
    args = [fqdn, state["settings"]["hostname"], people, EXAMPLE_HOST, EXAMPLE_NAMES]

    async with async_playwright() as p:
        browser = await p.chromium.launch()

        async def shoot(name: str, url: str, viewport: dict, cookie_host: str | None = VM_HOST, full: bool = True) -> None:
            context = await browser.new_context(viewport=viewport, device_scale_factor=2, color_scheme="light")
            if cookie_host:
                await context.add_cookies([{"name": "ingress_session", "value": cookie, "domain": cookie_host, "path": "/"}])
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle")
            if cookie_host:
                await page.reload(wait_until="networkidle")  # the hash picks the page on load
            await page.wait_for_timeout(2500)
            await page.evaluate(MASK, args)
            await page.screenshot(path=str(OUT / f"{name}.png"), full_page=full)
            await context.close()
            print("wrote", name)

        desktop = {"width": 1100, "height": 760}
        phone = {"width": 390, "height": 844}
        await shoot("welcome", f"{base}#welcome", desktop, full=False)
        await shoot("connection", f"{base}#connection", desktop)
        await shoot("hosted-linked", f"{base}#hosted", desktop, full=False)
        await shoot("overview", f"{base}#overview", desktop)
        await shoot("overview-phone", f"{base}#overview", phone, full=False)
        await shoot("link-code", claim["link_url"], {"width": 700, "height": 560}, cookie_host=None, full=False)
        await browser.close()


asyncio.run(main())
