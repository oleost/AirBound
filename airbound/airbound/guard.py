"""Auth guard: refuses external logins that finish without an MFA step.

Caddy routes only /auth/login_flow* here. Each response from Home Assistant's
login flow is inspected: a flow that reaches ``create_entry`` (which carries
the authorization code) without HA having asked for an MFA code is refused,
so no tokens can be issued for it.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable

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
        if kind == "form" and payload.get("step_id") in MFA_STEPS and flow_id:
            self._mfa_seen[flow_id] = self._clock()
            return True
        if kind == "create_entry":
            return self._mfa_seen.pop(flow_id, None) is not None if flow_id else False
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

    def __init__(self, ha_url: str, require_mfa: Callable[[], bool], listener: Listener | None = None) -> None:
        self._ha_url = ha_url.rstrip("/")
        self._require_mfa = require_mfa
        self._listener = listener
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

    async def _proxy(self, request: web.Request) -> web.StreamResponse:
        assert self._session is not None
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
        headers["Accept-Encoding"] = "identity"  # the body must be readable as JSON
        data = await request.read()
        ip = (request.headers.get("X-Forwarded-For") or request.remote or "").split(",")[0].strip()
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
                if payload.get("type") == "create_entry":
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
