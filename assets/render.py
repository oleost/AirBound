"""Render the logo SVG into the PNGs used by GitHub, Patreon and the Home Assistant add-on store.

Usage: python assets/render.py   (needs playwright with chromium)
"""

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

HERE = Path(__file__).parent
ROOT = HERE.parent
LOGO = (HERE / "logo.svg").read_text()

WORDMARK = f"""
<div style="display:flex;align-items:center;gap:{{gap}}px;font-family:'Segoe UI',system-ui,sans-serif">
  <div style="width:{{size}}px;height:{{size}}px">{LOGO.replace('width="512" height="512"', 'width="100%" height="100%"')}</div>
  <div>
    <div style="font-size:{{title}}px;font-weight:700;color:{{ink}};letter-spacing:-0.5px;line-height:1">AirBound</div>
    {{tagline}}
  </div>
</div>"""


def page(width: int, height: int, body: str, background: str = "transparent") -> str:
    return (
        f"<html><body style='margin:0;width:{width}px;height:{height}px;display:flex;align-items:center;"
        f"justify-content:center;background:{background}'>{body}</body></html>"
    )


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch()

        async def shot(name: str, width: int, height: int, html: str, transparent: bool = True) -> None:
            ctx = await browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1)
            pg = await ctx.new_page()
            await pg.set_content(html)
            await pg.screenshot(path=str(name), omit_background=transparent)
            await ctx.close()
            print("wrote", name)

        square = LOGO.replace('width="512" height="512"', 'width="100%" height="100%"')
        # Square logo: Patreon profile picture, GitHub social preview base, docs.
        await shot(HERE / "logo-512.png", 512, 512, page(512, 512, square))
        # Home Assistant add-on store: icon.png (square) and logo.png (wide wordmark).
        await shot(ROOT / "airbound" / "icon.png", 256, 256, page(256, 256, square))
        await shot(
            ROOT / "airbound" / "logo.png",
            500,
            200,
            page(500, 200, WORDMARK.format(gap=22, size=150, title=64, ink="#14324d", tagline="")),
        )
        # Patreon cover / GitHub banner.
        tagline = (
            "<div style='font-size:34px;color:#e8f4fd;margin-top:14px'>"
            "Secure remote access for Home Assistant. No VPN, no open ports.</div>"
        )
        await shot(
            HERE / "banner.png",
            1600,
            400,
            page(
                1600,
                400,
                WORDMARK.format(gap=48, size=220, title=110, ink="#ffffff", tagline=tagline),
                background="linear-gradient(120deg,#0d2a44 0%,#0A5FA3 60%,#1A9BF0 100%)",
            ),
            transparent=False,
        )
        await browser.close()


asyncio.run(main())
