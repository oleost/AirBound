"""Runs the controller: public web/API on one local port, the frps plugin on another.

    python -m airbound_relay            run
    python -m airbound_relay dns        point the domain and *.domain at this server (once)
"""

from __future__ import annotations

import asyncio
import logging
import sys

from aiohttp import ClientSession, web

from . import config as config_module
from .cloudflare import ensure_records
from .db import Database
from .frp import Plugin
from .patreon import Patreon, PatreonError
from .web import Debouncer, Web

_LOGGER = logging.getLogger("airbound_relay")
SYNC_INTERVAL = 3600


async def plugin_app(plugin: Plugin) -> web.Application:
    async def handle(request: web.Request) -> web.Response:
        body = await request.json()
        return web.json_response(plugin.handle(body.get("op", ""), body.get("content") or {}))

    app = web.Application()
    app.router.add_post("/frp", handle)
    return app


async def keep_in_sync(patreon: Patreon) -> None:
    while True:
        try:
            count = await patreon.sync_members()
            _LOGGER.info("Member sync: %d with relay access", count)
        except PatreonError as err:
            _LOGGER.warning("Member sync failed: %s", err)
        except Exception:
            _LOGGER.exception("Member sync failed")
        await asyncio.sleep(SYNC_INTERVAL)


async def run() -> None:
    config = config_module.load()
    config_module.STATE_DIR.mkdir(parents=True, exist_ok=True)
    db = Database(config_module.STATE_DIR / "controller.db")
    async with ClientSession() as session:
        patreon = Patreon(config, db, session)
        sync = Debouncer(patreon.sync_members)
        site = Web(config, db, patreon, sync.poke)
        runners = []
        for app, port in ((site.app(), config.web_port), (await plugin_app(Plugin(config, db)), config.plugin_port)):
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", port).start()
            runners.append(runner)
        _LOGGER.info("Controller for %s running", config.domain)
        try:
            await patreon.ensure_webhook()
        except Exception as err:  # the hourly sync still works without it
            _LOGGER.warning("Couldn't set up the Patreon webhook: %s", err)
        await keep_in_sync(patreon)


async def dns() -> None:
    config = config_module.load()
    async with ClientSession() as session:
        for line in await ensure_records(session, config):
            print(line)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    asyncio.run(dns() if command == "dns" else run())


if __name__ == "__main__":
    main()
