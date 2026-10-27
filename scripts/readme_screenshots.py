"""Screenshots of the panel for the README, taken from the test system and framed.

Read-only on the test system: it only navigates. Before each capture the real
address, people's names and IP addresses on the page are swapped for examples,
so nothing about the test system ends up in the repository. Every capture uses
the same window size and is then placed in the same browser (or phone) frame,
so the README's images line up.

    python scripts/readme_screenshots.py

Writes docs/screenshots/hero.png (computer and phone together) and four
gallery images of equal size.
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "integration"))
import aiohttp  # noqa: E402
from conftest import VM_HOST, ingress  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

OUT = ROOT / "docs" / "screenshots"
RAW = ROOT / "test-results" / "screenshots-raw"  # unmasked frames never leave test-results/ (gitignored)
EXAMPLE_HOST = "otter-comet-maple"
EXAMPLE_NAMES = ["Alex", "Sam", "Robin", "Kim", "Charlie"]
DESKTOP = {"width": 1280, "height": 800}
PHONE = {"width": 390, "height": 844}

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

STYLE = """
* { box-sizing: border-box; margin: 0; }
body { font-family: "Segoe UI", system-ui, -apple-system, sans-serif; }
.stage { display: flex; align-items: center; justify-content: center; }
.window { background: #fff; border-radius: 14px; overflow: hidden; box-shadow: 0 24px 60px rgba(9, 37, 66, .28), 0 2px 6px rgba(9, 37, 66, .12); }
.bar { height: 38px; background: #eef2f7; display: flex; align-items: center; padding: 0 14px; gap: 7px; border-bottom: 1px solid #dde4ec; }
.dot { width: 12px; height: 12px; border-radius: 50%; }
.title { margin-left: 14px; flex: 1; height: 24px; border-radius: 7px; background: #fff; color: #55697d; font-size: 13px; display: flex; align-items: center; padding: 0 12px; }
.window img, .phone img { display: block; width: 100%; }
.phone { background: #111; border-radius: 46px; padding: 12px; box-shadow: 0 24px 60px rgba(9, 37, 66, .35); }
.phone img { border-radius: 34px; }
"""


def data_uri(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()


def window(image: Path, title: str, width: int) -> str:
    dots = "".join(f'<span class="dot" style="background:{c}"></span>' for c in ("#ff5f57", "#febc2e", "#28c840"))
    return f'<div class="window" style="width:{width}px"><div class="bar">{dots}<span class="title">{title}</span></div><img src="{data_uri(image)}"></div>'


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)
    base, cookie = await ingress()
    async with aiohttp.ClientSession(cookies={"ingress_session": cookie}) as s:
        async with s.get(base + "api/state") as r:
            state = await r.json()
        async with s.get(base + "api/users") as r:
            people = [u["name"] for u in (await r.json())["users"]]
        async with s.post("https://airbound.dev/api/v1/claims", json={"install_id": "readme-screenshot-0001"}) as r:
            claim = await r.json()  # an unused link request, only to show the code page
    args = [state["settings"]["fqdn"], state["settings"]["hostname"], people, EXAMPLE_HOST, EXAMPLE_NAMES]

    async with async_playwright() as p:
        browser = await p.chromium.launch()

        async def capture(name: str, url: str, viewport: dict, panel: bool = True) -> Path:
            context = await browser.new_context(viewport=viewport, device_scale_factor=2, color_scheme="light")
            if panel:
                await context.add_cookies([{"name": "ingress_session", "value": cookie, "domain": VM_HOST, "path": "/"}])
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle")
            if panel:
                await page.reload(wait_until="networkidle")  # the hash picks the page on load
            await page.wait_for_timeout(2500)
            await page.evaluate(MASK, args)
            path = RAW / f"{name}.png"
            await page.screenshot(path=str(path))
            await context.close()
            return path

        overview = await capture("overview", f"{base}#overview", DESKTOP)
        phone = await capture("overview-phone", f"{base}#overview", PHONE)
        gallery = {
            "welcome": (await capture("welcome", f"{base}#welcome", DESKTOP), "Home Assistant · AirBound"),
            "connect": (await capture("connection", f"{base}#connection", DESKTOP), "Home Assistant · AirBound"),
            "address": (await capture("hosted", f"{base}#hosted", DESKTOP), "Home Assistant · AirBound"),
            "link": (await capture("link-code", claim["link_url"], DESKTOP, panel=False), "airbound.dev/link"),
        }

        async def compose(name: str, html: str, width: int, height: int) -> None:
            page = await browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
            await page.set_content(f"<style>{STYLE}</style>{html}")
            await page.wait_for_timeout(300)
            await page.screenshot(path=str(OUT / f"{name}.png"))
            await page.close()
            print("wrote", name)

        # The hero: the overview on a computer, with the phone in front of its right edge.
        await compose(
            "hero",
            '<div class="stage" style="width:1600px;height:900px;background:linear-gradient(135deg,#eaf4fd 0%,#d6e9fa 100%);position:relative">'
            f'<div style="position:absolute;left:90px;top:80px">{window(overview, "Home Assistant · AirBound", 1180)}</div>'
            f'<div class="phone" style="position:absolute;right:90px;top:120px;width:330px"><img src="{data_uri(phone)}"></div></div>',
            1600, 900,
        )
        # The gallery: four frames of exactly the same size.
        for name, (image, title) in gallery.items():
            await compose(
                f"gallery-{name}",
                f'<div class="stage" style="width:1200px;height:820px;background:#f4f7fb">{window(image, title, 1100)}</div>',
                1200, 820,
            )
        await browser.close()


asyncio.run(main())
