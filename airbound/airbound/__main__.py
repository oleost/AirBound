"""Entry point: starts the auth guard, the AirBound panel and, once set up, the proxy."""

from __future__ import annotations

import asyncio
import logging
import signal

from aiohttp import ClientSession, web

from .caddy import GUARD_ADDRESS, Caddy
from .crowdsec import CrowdSec, log_failed_login
from .golive import GoLive, keep_dns_current, keep_port_open
from .guard import AuthGuard
from .relay import Frpc
from .reporting import Reporter
from .settings import Store
from .supervisor import Supervisor
from .web import Panel

_LOGGER = logging.getLogger("airbound")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    store = Store()
    if store.settings.live and store.settings.tls == "cloudflare" and not store.settings.public_ip:
        # Marked live without the DNS step ever running (left over from v0.2.2): go live again.
        store.update(live=False)
    if store.settings.live and not store.settings.mode:
        store.update(mode="direct")  # set up before relay mode existed
    session = ClientSession()
    supervisor = Supervisor(session)
    ha_upstream = await supervisor.ha_upstream()

    crowdsec = CrowdSec()
    caddy = Caddy(bouncer_key=lambda: crowdsec.bouncer_key)
    frpc = Frpc()
    reporter = Reporter(store, supervisor, session, frpc, crowdsec)

    def on_guard_event(event: str, info: dict) -> None:
        reporter.on_guard_event(event, info)
        if event == "failed":
            log_failed_login(info["ip"], info["path"], info["user_agent"])

    guard = AuthGuard(f"http://{ha_upstream}", require_mfa=lambda: store.settings.require_mfa, listener=on_guard_event)
    golive = GoLive(store, caddy, frpc, session, ha_upstream)
    panel = Panel(store, supervisor, session, caddy, frpc, crowdsec, guard, golive, ha_upstream)

    runners = []
    guard_host, guard_port = GUARD_ADDRESS.split(":")
    for app, host, port in ((guard.app(), guard_host, int(guard_port)), (panel.app(), "0.0.0.0", 8099)):
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        runners.append(runner)

    await crowdsec.start()
    if store.settings.live and store.settings.is_configured:
        await caddy.apply(store.settings, ha_upstream)
        if store.settings.mode == "relay":
            await frpc.apply(store.settings)
    else:
        _LOGGER.info("Not live yet: open AirBound in the Home Assistant sidebar to set it up")
    ddns = asyncio.create_task(keep_dns_current(store, session))
    reporting = asyncio.create_task(reporter.run())
    portmapping = asyncio.create_task(keep_port_open(store, session, supervisor))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    ddns.cancel()
    reporting.cancel()
    portmapping.cancel()
    await frpc.stop()
    await caddy.stop()
    await crowdsec.stop()
    for runner in runners:
        await runner.cleanup()
    await session.close()


if __name__ == "__main__":
    asyncio.run(main())
