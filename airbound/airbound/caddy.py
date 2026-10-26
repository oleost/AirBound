"""Caddy configuration and process management."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path

from .crowdsec import LAPI as CROWDSEC_LAPI
from .relay import RELAY_LISTENER
from .settings import Settings

_LOGGER = logging.getLogger(__name__)

CADDYFILE = Path("/data/Caddyfile")
ACCESS_LOG = Path("/data/logs/access.log")
GUARD_ADDRESS = "127.0.0.1:8100"


def _site(address: str, settings: Settings, ha_upstream: str, crowdsec: bool, guard_forged: bool = False) -> str:
    bouncer = "\n\t\t\tcrowdsec" if crowdsec else ""
    # On the relay listener every real client comes from the internet. A PROXY
    # header claiming a private or loopback address can only be forged (by the
    # relay's operator or someone posing as the relay); drop it, so nobody can
    # pass for a device on the home network.
    forged = (
        """
			@forged_local remote_ip private_ranges
			abort @forged_local"""
        if guard_forged
        else ""
    )
    return f"""{address} {{
	log {{
		output file {ACCESS_LOG} {{
			roll_size 10MiB
			roll_keep 5
		}}
		format json
	}}

	@airbound host {settings.fqdn}
	handle @airbound {{
		route {{{forged}{bouncer}
			header Strict-Transport-Security "max-age=31536000"
			@login path /auth/login_flow /auth/login_flow/*
			reverse_proxy @login {GUARD_ADDRESS}
			reverse_proxy {ha_upstream}
		}}
	}}

	handle {{
		abort
	}}
}}
"""


def render_caddyfile(settings: Settings, ha_upstream: str, crowdsec: bool = False) -> str:
    """Build the Caddyfile for the current settings.

    The site is a wildcard for the whole domain, so only ``*.domain`` is ever
    issued and the real hostname never shows up in Certificate Transparency
    logs. Requests for any other name under the domain are dropped.

    In relay mode a second, local-only listener takes traffic from frpc. It
    reads the PROXY protocol header frpc adds, so HA sees the real client, and
    drops headers that claim a private address.

    How certificates are issued is set globally (acme_dns / local_certs), so
    the two site blocks share one certificate policy; per-site tls blocks for
    the same wildcard would be two policies, which Caddy refuses.
    """
    _relay_host, relay_port = RELAY_LISTENER.split(":")
    # {$VAR} is filled in when the config is loaded, so a new token applies on reload.
    issuing = "local_certs" if settings.tls == "internal" else "acme_dns cloudflare {$CF_API_TOKEN}"
    relay = settings.mode == "relay"
    # The servers block must name the exact listener address.
    relay_server = (
        f"""
	servers :{relay_port} {{
		listener_wrappers {{
			proxy_protocol {{
				allow 127.0.0.1/32
			}}
			tls
		}}
	}}"""
        if relay
        else ""
    )
    # Without hard fails, Caddy keeps serving if CrowdSec is down; it never locks anyone out.
    bouncer = (
        f"""
	crowdsec {{
		api_url http://{CROWDSEC_LAPI}
		api_key {{$CROWDSEC_API_KEY}}
		ticker_interval 15s
	}}"""
        if crowdsec
        else ""
    )
    config = f"""{{
	admin 127.0.0.1:2019
	auto_https disable_redirects
	skip_install_trust
	storage file_system /data/caddy
	{issuing}
	servers {{
		protocols h1 h2
	}}{relay_server}{bouncer}
}}

{_site(f"*.{settings.domain}:443", settings, ha_upstream, crowdsec)}"""
    if relay:
        # The relay port isn't published outside the container, and PROXY headers
        # are trusted only from frpc on 127.0.0.1.
        config += "\n" + _site(f"*.{settings.domain}:{relay_port}", settings, ha_upstream, crowdsec, guard_forged=True)
    return config


class Caddy:
    """Runs Caddy as a child process and reloads it on config changes."""

    def __init__(self, bouncer_key: Callable[[], str] = lambda: "") -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._bouncer_key = bouncer_key

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def apply(self, settings: Settings, ha_upstream: str) -> None:
        ACCESS_LOG.parent.mkdir(parents=True, exist_ok=True)
        key = self._bouncer_key()
        CADDYFILE.write_text(render_caddyfile(settings, ha_upstream, crowdsec=bool(key)))
        env = {**os.environ, "CF_API_TOKEN": settings.cloudflare_token, "CROWDSEC_API_KEY": key}
        if self.running:
            await self._run("caddy", "reload", "--config", str(CADDYFILE), "--adapter", "caddyfile", env=env)
            return
        self._process = await asyncio.create_subprocess_exec(
            "caddy", "run", "--config", str(CADDYFILE), "--adapter", "caddyfile", env=env
        )
        _LOGGER.info("Caddy started for %s", settings.fqdn)

    async def stop(self) -> None:
        if self.running:
            assert self._process is not None
            self._process.terminate()
            await self._process.wait()

    @staticmethod
    async def _run(*cmd: str, env: dict[str, str]) -> None:
        proc = await asyncio.create_subprocess_exec(*cmd, env=env)
        if await proc.wait() != 0:
            raise RuntimeError(f"{' '.join(cmd[:2])} failed")
