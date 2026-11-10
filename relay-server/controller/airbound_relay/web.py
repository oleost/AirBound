"""Public pages and the API Home Assistant uses to link itself.

Linking works like signing in a TV: Home Assistant asks for a link request and
shows a short code, the person opens the link page, checks the code, logs in
with Patreon or GitHub, and Home Assistant picks up its key. The code check stops someone
from sending a stranger a link that would tie the stranger's membership to the
sender's Home Assistant.

The login only starts from the page that shows the code, in the browser that
opened it: the page sets a cookie and its buttons post the same value back, and
the login's answer is only accepted in that browser. So a link straight to the
login, or a page on another site, can't skip the code. And a membership that
already has all its Home Assistants never loses one by itself: the person picks
which one the new one replaces.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import logging
import re
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path

from aiohttp import web

from . import access, ops, pages
from .config import Config
from .db import Database, Device, DeviceLimit
from . import github as gh
from .github import GitHub, GitHubError
from .patreon import Patreon, PatreonError, authorize_url, valid_signature

_LOGGER = logging.getLogger(__name__)

HUB_CA = Path("/etc/airbound-hub/ca.crt")
TOUCH_ICON = Path(__file__).with_name("apple-touch-icon.png")
SERVER_NAME = "airbound-hosted-relay"  # the name in the relay's certificate that frpc expects
INSTALL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
OAUTH_STATE_TTL = 900
# __Host-: only this exact site can set it, so no address under the domain (each is someone's Home Assistant) can plant one.
LINK_COOKIE = "__Host-airbound-link"
BROWSER_RE = re.compile(r"[A-Za-z0-9_-]{20,64}")
WEEK = 7 * 86400


def claim_code(claim_id: str) -> str:
    """Six characters shown both in Home Assistant and on the link page."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    value = int.from_bytes(hashlib.sha256(claim_id.encode()).digest()[:5], "big")
    chars = [alphabet[(value >> (5 * i)) & 31] for i in range(6)]
    return "".join(chars[:3]) + "-" + "".join(chars[3:])


def limit_key(client: str) -> str:
    """Who a request counts against: the address, or for IPv6 its /64, since one customer has all of that."""
    try:
        address = ipaddress.ip_address(client)
    except ValueError:
        return client[:64]
    if address.version == 6:
        return str(ipaddress.ip_network((address, 64), strict=False).network_address)
    return str(address)


class RateLimit:
    """At most `count` requests per `window` seconds per client address."""

    def __init__(self, count: int, window: float) -> None:
        self._count = count
        self._window = window
        self._hits: dict[str, deque] = defaultdict(deque)
        self._swept = 0.0

    def _sweep(self, now: float) -> None:
        """Forget addresses with nothing left in the window, so the table can't grow without end."""
        self._swept = now
        for client in [c for c, hits in self._hits.items() if not hits or hits[-1] < now - self._window]:
            del self._hits[client]

    def allow(self, client: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if now - self._swept > self._window:
            self._sweep(now)
        hits = self._hits[limit_key(client)]
        while hits and hits[0] < now - self._window:
            hits.popleft()
        if len(hits) >= self._count:
            return False
        hits.append(now)
        return True


class Web:
    def __init__(self, config: Config, db: Database, patreon: Patreon, sync_soon, github: GitHub | None = None) -> None:
        self._config = config
        self._db = db
        self._patreon = patreon
        self._github = github
        self._sync_soon = sync_soon
        self._states: dict[str, tuple[str, str, float]] = {}  # OAuth state -> (claim, browser, expires)
        self._choices: dict[str, tuple[str, str, float]] = {}  # claim -> (account, browser, expires): picking the device to replace
        self._claims_limit = RateLimit(10, 3600)
        self._poll_limit = RateLimit(600, 3600)

    def app(self) -> web.Application:
        app = web.Application(client_max_size=256 * 1024)
        app.router.add_get("/", self.home)
        app.router.add_get("/privacy", self.privacy)
        app.router.add_get("/terms", self.terms)
        app.router.add_get("/relay-ca.pem", self.ca)
        app.router.add_get("/favicon.svg", self.icon)
        # Browsers and iPhones ask for these names even when no page links them.
        for name in ("/favicon.ico", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
            app.router.add_get(name, self.touch_icon)
        app.router.add_post("/api/v1/claims", self.new_claim)
        app.router.add_get("/api/v1/claims/{claim}", self.poll_claim)
        app.router.add_get("/api/v1/device", self.device_status)
        app.router.add_delete("/api/v1/device", self.device_unlink)
        app.router.add_get("/api/v1/ops", self.ops_report)
        app.router.add_get("/link/{claim}", self.link_page)
        app.router.add_post("/link/{claim}/login", self.link_login)
        app.router.add_post("/link/{claim}/replace", self.link_replace)
        app.router.add_get("/patreon/callback", self.callback)
        app.router.add_post("/patreon/webhook", self.webhook)
        app.router.add_post("/link/{claim}/github", self.link_github)
        app.router.add_get("/github/callback", self.github_callback)
        app.router.add_post("/github/webhook", self.github_webhook)
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

    async def terms(self, _request: web.Request) -> web.Response:
        return self._page("Terms", pages.terms(self._config))

    async def ca(self, _request: web.Request) -> web.Response:
        return web.Response(text=HUB_CA.read_text() if HUB_CA.exists() else "", content_type="application/x-pem-file")

    async def icon(self, _request: web.Request) -> web.Response:
        return web.Response(text=pages.ICON, content_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})

    async def touch_icon(self, _request: web.Request) -> web.Response:
        return web.Response(body=TOUCH_ICON.read_bytes(), content_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

    # API for Home Assistant

    async def new_claim(self, request: web.Request) -> web.Response:
        if not self._claims_limit.allow(self._client(request)):
            return web.json_response({"error": "Too many link requests. Try again in an hour."}, status=429)
        try:
            body = await request.json()
        except ValueError:
            body = None
        install_id = str(body.get("install_id", "")) if isinstance(body, dict) else ""
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
            return web.json_response({"state": "needs_membership", "join_url": self._config.join_url_for(claim["patreon_id"] or "")})
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

    def _device_for(self, request: web.Request) -> tuple[Device | None, web.Response | None]:
        """The device whose key and machine the add-on sent, or the answer when there is none."""
        key = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        device = self._db.device_by_key(key) if key else None
        if device is None:
            return None, web.json_response({"state": "unlinked"}, status=404)
        if not self._db.on_machine(device, request.headers.get("X-AirBound-Machine", "")):
            return None, web.json_response({"state": "other_machine"}, status=409)
        return device, None

    async def device_status(self, request: web.Request) -> web.Response:
        device, refused = self._device_for(request)
        if refused:
            return refused
        account = self._db.account(device.patreon_id)
        state = access.state(account, device.patreon_id, self._config.free_users, self._config.grace_days)
        return web.json_response(
            {
                "state": state,
                "fqdn": f"{device.hostname}.{self._config.domain}",
                "grace_until": access.grace_until(account, self._config.grace_days),
                "join_url": self._config.join_url_for(device.patreon_id),
                "manage_url": self._config.manage_url_for(device.patreon_id),
                "foreign_seen": device.foreign_seen,
            }
        )

    async def device_unlink(self, request: web.Request) -> web.Response:
        """The add-on's "Remove AirBound": frees the address and the device slot.
        Only from the machine that uses the key, so a copied backup can't take an address away."""
        device, refused = self._device_for(request)
        if refused:
            return refused
        self._db.revoke(device.id)
        return web.json_response({"state": "unlinked"})

    async def ops_report(self, request: web.Request) -> web.Response:
        """The relay's health for the weekly maintenance report; only with the operator's token."""
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not self._config.ops_token or not secrets.compare_digest(token.encode(), self._config.ops_token.encode()):
            return web.json_response({"error": "not found"}, status=404)
        days = await asyncio.to_thread(ops.certificate_days_left, "127.0.0.1", self._config.domain)
        return web.json_response(ops.report(self._config, self._db, days))  # the database stays on this thread

    # Pages the person sees

    @staticmethod
    def _browser(request: web.Request) -> str:
        """The value this browser got from the code page; empty if it never opened one."""
        value = request.cookies.get(LINK_COOKIE, "")
        return value if BROWSER_RE.fullmatch(value) else ""

    async def _posted_from_code_page(self, request: web.Request) -> str:
        """The browser's value if this request was posted by a button on our own page in that browser, else empty."""
        browser = self._browser(request)
        token = str((await request.post()).get("token", ""))
        return browser if browser and secrets.compare_digest(token.encode(), browser.encode()) else ""

    async def link_page(self, request: web.Request) -> web.Response:
        claim = self._db.claim(request.match_info["claim"])
        if claim is None or claim["state"] in ("approved", "delivered"):
            return self._page("Link expired", pages.expired(), status=404)
        github = bool(self._github and self._github.enabled)
        browser = self._browser(request) or secrets.token_urlsafe(24)
        response = self._page("Link your Home Assistant", pages.confirm_code(claim["id"], claim_code(claim["id"]), browser, github=github))
        response.set_cookie(LINK_COOKIE, browser, max_age=3600, path="/", secure=True, httponly=True, samesite="Lax")
        return response

    def _new_state(self, claim_id: str, browser: str) -> str:
        now = time.time()
        self._states = {s: v for s, v in self._states.items() if v[2] > now}
        state = secrets.token_urlsafe(24)
        self._states[state] = (claim_id, browser, now + OAUTH_STATE_TTL)
        return state

    def _claim_for_state(self, request: web.Request):
        """(claim, browser) for a login's answer, only in the browser that started that login."""
        claim_id, browser, expires = self._states.pop(request.query.get("state", ""), ("", "", 0))
        if expires <= time.time() or not browser or not secrets.compare_digest(self._browser(request).encode(), browser.encode()):
            return None, ""
        claim = self._db.claim(claim_id)
        if claim is None or claim["state"] not in ("pending", "needs_membership") or "code" not in request.query:
            return None, ""
        return claim, browser

    async def _start_login(self, request: web.Request, where: str, authorize) -> web.Response:
        claim = self._db.claim(request.match_info["claim"])
        if claim is None or claim["state"] not in ("pending", "needs_membership"):
            return self._page("Link expired", pages.expired(), status=404)
        browser = await self._posted_from_code_page(request)
        if not browser:
            return self._page("Open the link again", pages.reopen(claim["id"]), status=403)
        url = authorize(self._config, self._new_state(claim["id"], browser))
        # A page that sends the browser on, not a redirect: the browser treats a redirect after a
        # form as part of the form, and the content security policy would have to list every hop.
        return web.Response(
            text=pages.page(f"Continue with {where}", pages.forward(url, where), self._config, head=pages.refresh(url)),
            content_type="text/html",
            headers=pages.HEADERS,
        )

    async def link_login(self, request: web.Request) -> web.Response:
        return await self._start_login(request, "Patreon", authorize_url)

    async def link_github(self, request: web.Request) -> web.Response:
        if not (self._github and self._github.enabled):
            return self._page("Link expired", pages.expired(), status=404)
        return await self._start_login(request, "GitHub", gh.authorize_url)

    def _link(self, claim, account: str, replace: str | None = None) -> web.Response:
        device, key = self._db.link_device(account, claim["install_id"], self._config.max_devices, replace=replace)
        self._db.set_claim(claim["id"], "approved", patreon_id=account, device_id=device.id, device_key=key)
        _LOGGER.info("Linked device %s", device.id)
        return self._page("Linked", pages.linked(f"{device.hostname}.{self._config.domain}"))

    def _finish_link(self, claim, account: str, entitled: bool, browser: str) -> web.Response:
        """After either login: link the Home Assistant, or ask for the support that gives access."""
        self._db.set_entitled(account, entitled)
        state_now = access.state(self._db.account(account), account, self._config.free_users, self._config.grace_days)
        if state_now == access.EXPIRED:
            self._db.set_claim(claim["id"], "needs_membership", patreon_id=account)
            if gh.is_github(account):
                return self._page("Sponsor AirBound", pages.needs_sponsorship(self._config, claim["id"], browser))
            return self._page("Start your membership", pages.needs_membership(self._config, claim["id"], browser))
        devices = self._db.devices_of(account)
        if not any(d.install_id == claim["install_id"] for d in devices):
            now = time.time()
            if self._db.new_devices_since(account, now - WEEK) >= self._config.new_devices_per_week:
                return self._page("Too many new addresses", pages.too_many(), status=429)
            if len(devices) >= self._config.max_devices:
                self._choices = {c: v for c, v in self._choices.items() if v[2] > now}
                self._choices[claim["id"]] = (account, browser, now + OAUTH_STATE_TTL)
                return self._page("Choose which one to replace", pages.choose_device(self._config, claim["id"], browser, devices, now))
        return self._link(claim, account)

    async def link_replace(self, request: web.Request) -> web.Response:
        """The person picked which of their Home Assistants the new one replaces."""
        claim = self._db.claim(request.match_info["claim"])
        browser = await self._posted_from_code_page(request)
        account, chose_in, expires = self._choices.get(claim["id"] if claim is not None else "", ("", "", 0))
        if claim is None or claim["state"] != "pending" or not browser or browser != chose_in or expires <= time.time():
            return self._page("Link expired", pages.expired(), status=404)
        del self._choices[claim["id"]]
        try:
            return self._link(claim, account, replace=str((await request.post()).get("device", "")))
        except DeviceLimit:
            return self._page("Link expired", pages.expired(), status=404)

    async def callback(self, request: web.Request) -> web.Response:
        claim, browser = self._claim_for_state(request)
        if claim is None:
            return self._page("Link expired", pages.expired(), status=400)
        try:
            token = await self._patreon.exchange_code(request.query["code"])
            patreon_id, entitled = await self._patreon.check_person(token)
        except PatreonError as err:
            _LOGGER.warning("Patreon login failed: %s", err)
            return self._page("Something went wrong", pages.error("Patreon didn't confirm the login. Please try again."), status=502)
        return self._finish_link(claim, patreon_id, entitled, browser)

    async def github_callback(self, request: web.Request) -> web.Response:
        claim, browser = self._claim_for_state(request)
        if claim is None or not (self._github and self._github.enabled):
            return self._page("Link expired", pages.expired(), status=400)
        try:
            token = await self._github.exchange_code(request.query["code"])
            account, entitled = await self._github.check_person(token)
        except GitHubError as err:
            _LOGGER.warning("GitHub login failed: %s", err)
            return self._page("Something went wrong", pages.error("GitHub didn't confirm the login. Please try again."), status=502)
        return self._finish_link(claim, account, entitled, browser)

    async def github_webhook(self, request: web.Request) -> web.Response:
        body = await request.read()
        secret = self._config.github_webhook_secret
        if not secret or not gh.valid_signature(secret, body, request.headers.get("X-Hub-Signature-256", "")):
            return web.json_response({"error": "bad signature"}, status=403)
        _LOGGER.info("GitHub webhook: %s", html.escape(request.headers.get("X-GitHub-Event", "")))
        self._sync_soon()
        return web.json_response({"ok": True})

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
