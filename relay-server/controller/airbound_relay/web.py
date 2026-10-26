"""Public pages and the API Home Assistant uses to link itself.

Linking works like signing in a TV: Home Assistant asks for a link request and
shows a short code, the person opens the link page, checks the code, logs in
with Patreon, and Home Assistant picks up its key. The code check stops someone
from sending a stranger a link that would tie the stranger's membership to the
sender's Home Assistant.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path

from aiohttp import web

from . import access, pages
from .config import Config
from .db import Database
from .patreon import Patreon, PatreonError, authorize_url, valid_signature

_LOGGER = logging.getLogger(__name__)

HUB_CA = Path("/etc/airbound-hub/ca.crt")
SERVER_NAME = "airbound-hosted-relay"  # the name in the relay's certificate that frpc expects
INSTALL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
OAUTH_STATE_TTL = 900


def claim_code(claim_id: str) -> str:
    """Six characters shown both in Home Assistant and on the link page."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    value = int.from_bytes(hashlib.sha256(claim_id.encode()).digest()[:5], "big")
    chars = [alphabet[(value >> (5 * i)) & 31] for i in range(6)]
    return "".join(chars[:3]) + "-" + "".join(chars[3:])


class RateLimit:
    """At most `count` requests per `window` seconds per client address."""

    def __init__(self, count: int, window: float) -> None:
        self._count = count
        self._window = window
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, client: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        hits = self._hits[client]
        while hits and hits[0] < now - self._window:
            hits.popleft()
        if len(hits) >= self._count:
            return False
        hits.append(now)
        return True


class Web:
    def __init__(self, config: Config, db: Database, patreon: Patreon, sync_soon) -> None:
        self._config = config
        self._db = db
        self._patreon = patreon
        self._sync_soon = sync_soon
        self._states: dict[str, tuple[str, float]] = {}
        self._claims_limit = RateLimit(10, 3600)
        self._poll_limit = RateLimit(600, 3600)

    def app(self) -> web.Application:
        app = web.Application(client_max_size=256 * 1024)
        app.router.add_get("/", self.home)
        app.router.add_get("/privacy", self.privacy)
        app.router.add_get("/relay-ca.pem", self.ca)
        app.router.add_post("/api/v1/claims", self.new_claim)
        app.router.add_get("/api/v1/claims/{claim}", self.poll_claim)
        app.router.add_get("/api/v1/device", self.device_status)
        app.router.add_get("/link/{claim}", self.link_page)
        app.router.add_get("/link/{claim}/login", self.link_login)
        app.router.add_get("/patreon/callback", self.callback)
        app.router.add_post("/patreon/webhook", self.webhook)
        return app

    @staticmethod
    def _client(request: web.Request) -> str:
        # Caddy, on the same machine, puts the real client first.
        return request.headers.get("X-Forwarded-For", request.remote or "").split(",")[0].strip()

    def _page(self, title: str, body: str, status: int = 200) -> web.Response:
        return web.Response(text=pages.page(title, body, self._config), content_type="text/html", status=status, headers=pages.HEADERS)

    async def home(self, _request: web.Request) -> web.Response:
        return self._page("Secure remote access for Home Assistant", pages.home(self._config))

    async def privacy(self, _request: web.Request) -> web.Response:
        return self._page("Privacy", pages.privacy(self._config))

    async def ca(self, _request: web.Request) -> web.Response:
        return web.Response(text=HUB_CA.read_text() if HUB_CA.exists() else "", content_type="application/x-pem-file")

    # API for Home Assistant

    async def new_claim(self, request: web.Request) -> web.Response:
        if not self._claims_limit.allow(self._client(request)):
            return web.json_response({"error": "Too many link requests. Try again in an hour."}, status=429)
        try:
            install_id = str((await request.json()).get("install_id", ""))
        except ValueError:
            install_id = ""
        if not INSTALL_ID_RE.match(install_id):
            return web.json_response({"error": "bad request"}, status=400)
        claim_id, poll = self._db.new_claim(install_id)
        return web.json_response(
            {
                "claim": claim_id,
                "poll": poll,
                "code": claim_code(claim_id),
                "link_url": f"{self._config.base_url}/link/{claim_id}",
                "interval": 3,
                "expires_in": 900,
            }
        )

    async def poll_claim(self, request: web.Request) -> web.Response:
        if not self._poll_limit.allow(self._client(request)):
            return web.json_response({"error": "slow down"}, status=429)
        poll = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        claim = self._db.claim_for_poll(request.match_info["claim"], poll)
        if claim is None:
            return web.json_response({"state": "expired"}, status=404)
        if claim["state"] == "needs_membership":
            return web.json_response({"state": "needs_membership", "join_url": self._config.join_url})
        if claim["state"] == "approved" and claim["device_key"]:
            device = self._db.device(claim["device_id"])
            self._db.take_claim_key(claim["id"])
            return web.json_response(
                {
                    "state": "approved",
                    "device": {"id": device.id, "key": claim["device_key"], "hostname": device.hostname, "domain": self._config.domain},
                    "relay": {
                        "host": self._config.relay_host,
                        "port": self._config.relay_port,
                        "server_name": SERVER_NAME,
                        "ca": HUB_CA.read_text() if HUB_CA.exists() else "",
                    },
                }
            )
        return web.json_response({"state": claim["state"]})

    async def device_status(self, request: web.Request) -> web.Response:
        key = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        device = self._db.device_by_key(key) if key else None
        if device is None:
            return web.json_response({"state": "unlinked"}, status=404)
        account = self._db.account(device.patreon_id)
        state = access.state(account, device.patreon_id, self._config.free_users, self._config.grace_days)
        return web.json_response(
            {
                "state": state,
                "fqdn": f"{device.hostname}.{self._config.domain}",
                "grace_until": access.grace_until(account, self._config.grace_days),
                "join_url": self._config.join_url,
                "manage_url": self._config.manage_url,
            }
        )

    # Pages the person sees

    async def link_page(self, request: web.Request) -> web.Response:
        claim = self._db.claim(request.match_info["claim"])
        if claim is None or claim["state"] in ("approved", "delivered"):
            return self._page("Link expired", pages.expired(), status=404)
        return self._page("Link your Home Assistant", pages.confirm_code(claim["id"], claim_code(claim["id"])))

    async def link_login(self, request: web.Request) -> web.Response:
        claim = self._db.claim(request.match_info["claim"])
        if claim is None:
            return self._page("Link expired", pages.expired(), status=404)
        now = time.time()
        self._states = {s: v for s, v in self._states.items() if v[1] > now}
        state = secrets.token_urlsafe(24)
        self._states[state] = (claim["id"], now + OAUTH_STATE_TTL)
        raise web.HTTPFound(authorize_url(self._config, state))

    async def callback(self, request: web.Request) -> web.Response:
        state = request.query.get("state", "")
        claim_id, expires = self._states.pop(state, ("", 0))
        claim = self._db.claim(claim_id) if expires > time.time() else None
        if claim is None or "code" not in request.query:
            return self._page("Link expired", pages.expired(), status=400)
        try:
            token = await self._patreon.exchange_code(request.query["code"])
            patreon_id, entitled = await self._patreon.check_person(token)
        except PatreonError as err:
            _LOGGER.warning("Patreon login failed: %s", err)
            return self._page("Something went wrong", pages.error("Patreon didn't confirm the login. Please try again."), status=502)
        self._db.set_entitled(patreon_id, entitled)
        state_now = access.state(self._db.account(patreon_id), patreon_id, self._config.free_users, self._config.grace_days)
        if state_now == access.EXPIRED:
            self._db.set_claim(claim_id, "needs_membership", patreon_id=patreon_id)
            return self._page("Start your membership", pages.needs_membership(self._config, claim_id))
        device, key = self._db.link_device(patreon_id, claim["install_id"], self._config.max_devices)
        self._db.set_claim(claim_id, "approved", patreon_id=patreon_id, device_id=device.id, device_key=key)
        _LOGGER.info("Linked device %s", device.id)
        return self._page("Linked", pages.linked(f"{device.hostname}.{self._config.domain}"))

    async def webhook(self, request: web.Request) -> web.Response:
        body = await request.read()
        secret = self._db.get("webhook_secret") or ""
        if not secret or not valid_signature(secret, body, request.headers.get("X-Patreon-Signature", "")):
            return web.json_response({"error": "bad signature"}, status=403)
        _LOGGER.info("Patreon webhook: %s", html.escape(request.headers.get("X-Patreon-Event", "")))
        self._sync_soon()
        return web.json_response({"ok": True})


class Debouncer:
    """Runs a coroutine soon after being poked, at most once per `delay` seconds."""

    def __init__(self, func, delay: float = 10) -> None:
        self._func = func
        self._delay = delay
        self._task: asyncio.Task | None = None

    def poke(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        await asyncio.sleep(self._delay)
        try:
            await self._func()
        except Exception:
            _LOGGER.exception("Member sync failed")
