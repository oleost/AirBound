"""Auth guard: refuses external logins that finish without an MFA step, or that are for an unknown app.

Caddy routes only /auth/login_flow* here. Each response from Home Assistant's
login flow is inspected: a flow that reaches ``create_entry`` (which carries
the authorization code) without HA having asked for an MFA code is refused,
so no tokens can be issued for it. Any answer that carries a ``result`` counts
as a finished login, whatever its type is called, so a renamed type in a later
Home Assistant can't open a way around the check.

Each login also names the app it is for, its ``client_id``, and HA sends the
authorization code to that app. A link to the real login page with another
site's client_id would hand that site a login, MFA included, if the person logs
in. So only this address, the official apps and what the person allowed may
start or continue a login from outside.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import time
from collections.abc import Callable, Collection

from aiohttp import ClientSession, web

_LOGGER = logging.getLogger(__name__)

MFA_STEPS = frozenset({"mfa", "select_mfa_module"})
FLOW_TTL = 15 * 60  # HA login flows and auth codes expire well within this
# HA's login page shows the message of a 403 response as "Error: <message>",
# the same way it shows HA's own "Login blocked" errors.
REFUSAL_MESSAGE = (
    "Remote login needs two-step verification (MFA). "
    "Turn it on at home under your profile → Security → Multi-factor authentication, then try again."
)

OFFICIAL_APPS = {
    "https://home-assistant.io/iOS": "Home Assistant app for iPhone, iPad and Mac",
    "https://home-assistant.io/android": "Home Assistant app for Android",
}
KNOWN_CLIENTS = {
    "https://pitangui.amazon.com/": "Alexa (US and Brazil)",
    "https://layla.amazon.com/": "Alexa (Europe)",
    "https://alexa.amazon.co.jp/": "Alexa (Japan and Australia)",
    "https://cast.home-assistant.io/": "Home Assistant Cast",
}
GOOGLE_ASSISTANT = "https://oauth-redirect.googleusercontent.com/r/"
MAX_BLOCKED = 10  # anyone who knows the address can add to this list, so it stays short
# Strict on purpose: a user name, a backslash or a query could make the browser and
# Home Assistant read different hosts out of the same address.
_CLIENT_RE = re.compile(r"(https?)://([a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)(?::(\d{1,5}))?(/[A-Za-z0-9._~/-]*)?", re.IGNORECASE)
_DEFAULT_PORTS = {"http": "80", "https": "443"}


def normalize_client(value: object) -> str | None:
    """The app's address in one spelling, or None if it isn't a plain web address."""
    if not isinstance(value, str) or len(value) > 2048 or not (match := _CLIENT_RE.fullmatch(value)):
        return None
    scheme, host, port, path = match.groups()
    scheme = scheme.lower()
    port = "" if port in (None, _DEFAULT_PORTS[scheme]) else f":{port}"
    return f"{scheme}://{host.lower()}{port}{path or '/'}"


def client_name(client: str) -> str:
    """A name for an app people may recognize; empty if it has none."""
    if client.startswith(GOOGLE_ASSISTANT):
        return "Google Assistant"
    return OFFICIAL_APPS.get(client) or KNOWN_CLIENTS.get(client, "")


def client_refusal(client: str | None) -> str:
    named = f" ({client})" if client else ""
    return (
        f"AirBound stopped a login for an app it doesn't know{named}. "
        "If you are connecting this app yourself, allow it in Home Assistant under AirBound → Allowed apps, then try again. "
        "If you didn't expect this, close this page: another site may be trying to get your login."
    )


def _unique(pairs: list[tuple[str, object]]) -> dict:
    """Refuses repeated keys, so nobody can show this check one client_id and Home Assistant another."""
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("repeated key")
    return dict(pairs)


def _client_of(data: bytes) -> object:
    try:
        body = json.loads(data, object_pairs_hook=_unique)
    except (ValueError, RecursionError):
        return None
    return body.get("client_id") if isinstance(body, dict) else None

# Hop-by-hop headers must not be forwarded by a proxy (RFC 9110, section 7.6.1).
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)


def finishes_login(payload: dict) -> bool:
    """Does this answer from the login flow hand out an authorization code?

    Home Assistant puts the code in ``result`` of a ``create_entry`` answer, and no other
    answer has a ``result``. Either sign is enough, so the check fails closed.
    """
    return payload.get("type") == "create_entry" or "result" in payload


class FlowTracker:
    """Remembers which login flows have shown an MFA step."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._mfa_seen: dict[str, float] = {}

    def allows(self, payload: object) -> bool:
        """Record a login flow response and return whether it may reach the client."""
        if not isinstance(payload, dict):
            return True
        self._expire()
        flow_id = payload.get("flow_id")
        kind = payload.get("type")
        if kind == "form" and payload.get("step_id") in MFA_STEPS and isinstance(flow_id, str) and flow_id and "result" not in payload:
            self._mfa_seen[flow_id] = self._clock()
            return True
        if finishes_login(payload):
            return self._mfa_seen.pop(flow_id, None) is not None if isinstance(flow_id, str) and flow_id else False
        return True

    def _expire(self) -> None:
        cutoff = self._clock() - FLOW_TTL
        for flow_id in [f for f, seen in self._mfa_seen.items() if seen < cutoff]:
            del self._mfa_seen[flow_id]


FAILED_LOGIN_ERRORS = frozenset({"invalid_auth", "invalid_code"})

# Listener for what happens at the door: ("login" | "refused" | "failed", {"ip", "username"}).
Listener = Callable[[str, dict], None]


class AuthGuard:
    """aiohttp application that proxies HA's login flow and enforces MFA."""

    def __init__(
        self,
        ha_url: str,
        require_mfa: Callable[[], bool],
        listener: Listener | None = None,
        allowed_clients: Callable[[], Collection[str] | None] = lambda: None,
    ) -> None:
        self._ha_url = ha_url.rstrip("/")
        self._require_mfa = require_mfa
        self._listener = listener
        self._allowed_clients = allowed_clients  # None: any app may log in
        self.blocked: dict[str, dict] = {}  # recently stopped apps, oldest first
        self._tracker = FlowTracker()
        self._usernames: dict[str, str] = {}  # flow_id -> username typed in that flow
        self._session: ClientSession | None = None
        self.refused = 0

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/auth/login_flow", self._proxy)
        app.router.add_route("*", "/auth/login_flow/{flow_id}", self._proxy)
        app.on_startup.append(self._start)
        app.on_cleanup.append(self._stop)
        return app

    async def _start(self, _app: web.Application) -> None:
        self._session = ClientSession(auto_decompress=False)

    async def _stop(self, _app: web.Application) -> None:
        if self._session:
            await self._session.close()

    def _emit(self, event: str, ip: str, flow_id: str | None, request: web.Request) -> None:
        if self._listener:
            self._listener(
                event,
                {
                    "ip": ip,
                    "username": self._usernames.get(flow_id or "", ""),
                    "path": request.path,
                    "user_agent": request.headers.get("User-Agent", ""),
                },
            )

    def _client_refused(self, request: web.Request, data: bytes, ip: str) -> web.Response | None:
        """The answer to a login for an app that isn't allowed; None if it may go on."""
        allowed = self._allowed_clients()
        if allowed is None or request.method != "POST":
            return None
        client = normalize_client(_client_of(data))
        # Caddy only lets this address through, and a site can't change the Host a browser sends.
        own = normalize_client(f"https://{request.host}/")
        if client is not None and (client == own or client in OFFICIAL_APPS or client in allowed):
            return None
        if client is not None:
            entry = self.blocked.pop(client, None)
            if entry is None:
                _LOGGER.warning("Stopped a login from %s for an unknown app: %s", ip, client)
            self.blocked[client] = {
                "client": client,
                "name": client_name(client),
                "count": (entry["count"] if entry else 0) + 1,
                "last": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            while len(self.blocked) > MAX_BLOCKED:
                del self.blocked[next(iter(self.blocked))]
        return web.json_response({"message": client_refusal(client)}, status=403)

    async def _proxy(self, request: web.Request) -> web.StreamResponse:
        assert self._session is not None
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
        headers["Accept-Encoding"] = "identity"  # the body must be readable as JSON
        if request.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
            # Browsers and the apps never compress what they send. A compressed body would be
            # one thing to this check and could be another to Home Assistant, so it stops here.
            return web.json_response({"message": "AirBound doesn't accept compressed login requests."}, status=415)
        data = await request.read()
        ip = (request.headers.get("X-Forwarded-For") or request.remote or "").split(",")[0].strip()
        if (refused := self._client_refused(request, data, ip)) is not None:
            return refused
        flow_id = request.match_info.get("flow_id")
        if flow_id and data:
            try:
                username = json.loads(data).get("username")
            except (ValueError, AttributeError):
                username = None
            if isinstance(username, str) and username:
                if len(self._usernames) > 1000:  # abandoned flows; keep memory bounded
                    self._usernames.clear()
                self._usernames[flow_id] = username[:100]
        async with self._session.request(
            request.method,
            f"{self._ha_url}{request.path_qs}",
            headers=headers,
            data=data,
            allow_redirects=False,
        ) as upstream:
            body = await upstream.read()
            out_headers = {
                k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP
            }
            status = upstream.status

        if status == 200 and upstream.content_type == "application/json":
            payload = json.loads(body)
            allowed = self._tracker.allows(payload)
            if isinstance(payload, dict):
                if finishes_login(payload):
                    if not allowed and self._require_mfa():
                        self.refused += 1
                        _LOGGER.warning("Refused external login without MFA from %s", ip)
                        self._emit("refused", ip, flow_id, request)
                        self._usernames.pop(flow_id or "", None)
                        return web.json_response({"message": REFUSAL_MESSAGE}, status=403)
                    self._emit("login", ip, flow_id, request)
                    self._usernames.pop(flow_id or "", None)
                elif (payload.get("errors") or {}).get("base") in FAILED_LOGIN_ERRORS:
                    self._emit("failed", ip, flow_id, request)

        return web.Response(status=status, body=body, headers=out_headers)
