"""Entry point: starts the auth guard, the AirBound panel and, once set up, the proxy."""

from __future__ import annotations

import asyncio
import datetime
import logging
import secrets
import signal
from pathlib import Path

from aiohttp import ClientSession, web

from .caddy import GUARD_ADDRESS, Caddy
from .certwatch import CertWatch
from .crowdsec import CrowdSec, log_failed_login
from .golive import GoLive, keep_dns_current, keep_port_open
from .guard import AuthGuard
from .mqtt import ENTITIES, Mqtt, entity_id
from .relay import Frpc
from .remove import Removal
from .reporting import Reporter
from .settings import Store
from .supervisor import Supervisor, SupervisorError
from .web import Panel

_LOGGER = logging.getLogger("airbound")
MQTT_MIGRATED = Path("/data/mqtt_migrated")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    store = Store()
    if store.settings.live and store.settings.tls == "cloudflare" and not store.settings.public_ip:
        # Marked live without the DNS step ever running (left over from v0.2.2): go live again.
        store.update(live=False)
    if store.settings.live and not store.settings.mode:
        store.update(mode="direct")  # set up before relay mode existed
    if not store.settings.install_id:
        store.update(install_id=secrets.token_urlsafe(24))
    if store.settings.tls == "hosted" and not store.settings.hosted_since:
        # Linked before 0.11.1 recorded the time; watch certificates from now on.
        store.update(hosted_since=datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    session = ClientSession()
    supervisor = Supervisor(session)
    ha_upstream = await supervisor.ha_upstream()

    crowdsec = CrowdSec()
    caddy = Caddy(bouncer_key=lambda: crowdsec.bouncer_key)
    frpc = Frpc()
    certwatch = CertWatch()
    broker_config = await supervisor.mqtt_service()
    slug = (await supervisor.get("/addons/self/info")).get("slug", "")
    broker = Mqtt(broker_config, slug) if broker_config else None
    if broker and not store.settings.removed:
        # Once, on the switch from REST to MQTT: plain REST states must be gone
        # before discovery, or Home Assistant gives the new entities IDs ending
        # in _2. Never again afterwards: by then the IDs belong to the MQTT
        # entities, and deleting their state blanks them until a value changes.
        if not MQTT_MIGRATED.exists():
            for key in ENTITIES:
                try:
                    await supervisor.core_delete(f"/states/{entity_id(key)}")
                except SupervisorError:
                    pass
            MQTT_MIGRATED.touch()
        broker.start()
    elif not broker:
        _LOGGER.info("No MQTT broker found; sensors go through the REST API instead")
    reporter = Reporter(store, supervisor, session, frpc, crowdsec, certwatch, broker)

    def on_guard_event(event: str, info: dict) -> None:
        reporter.on_guard_event(event, info)
        if event == "failed":
            log_failed_login(info["ip"], info["path"], info["user_agent"])

    guard = AuthGuard(f"http://{ha_upstream}", require_mfa=lambda: store.settings.require_mfa, listener=on_guard_event)
    golive = GoLive(store, caddy, frpc, session, ha_upstream)
    removal = Removal(store, supervisor, session, caddy, frpc, broker)
    panel = Panel(store, supervisor, session, caddy, frpc, crowdsec, certwatch, guard, golive, removal, ha_upstream)

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
        if store.settings.mode in ("relay", "hosted"):
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
    if broker:
        await broker.stop()
    portmapping.cancel()
    await frpc.stop()
    await caddy.stop()
    await crowdsec.stop()
    for runner in runners:
        await runner.cleanup()
    await session.close()


if __name__ == "__main__":
    asyncio.run(main())
