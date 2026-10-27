"""The AirBound panel: setup wizard and status page, served through HA ingress."""

from __future__ import annotations

import datetime
import io
import ipaddress
from pathlib import Path

import segno
from aiohttp import ClientSession, web

from . import checks, ha_bans, ha_settings, hosted, network, portmap, relay, steps, users
from .caddy import Caddy
from .certwatch import CertWatch
from .cloudflare import Cloudflare, CloudflareError
from .crowdsec import CrowdSec
from .golive import GoLive
from .guard import AuthGuard
from .settings import ALL_STEPS, COMMON_HOSTNAMES, Store, random_hostname, valid_hostname
from .supervisor import Supervisor

INGRESS_PROXY = "172.30.32.2"
STATIC = Path(__file__).parent / "static"


@web.middleware
async def ingress_only(request: web.Request, handler):
    if request.remote != INGRESS_PROXY:
        raise web.HTTPForbidden()
    return await handler(request)


@web.middleware
async def friendly_errors(request: web.Request, handler):
    try:
        return await handler(request)
    except (CloudflareError, hosted.HostedError) as err:
        return web.json_response({"error": str(err)}, status=400)


def user_id(request: web.Request) -> str | None:
    """HA ingress tells add-ons who is looking at the panel."""
    return request.headers.get("X-Remote-User-Id")


class Panel:
    def __init__(
        self,
        store: Store,
        supervisor: Supervisor,
        session: ClientSession,
        caddy: Caddy,
        frpc: relay.Frpc,
        crowdsec: CrowdSec,
        certwatch: CertWatch,
        guard: AuthGuard,
        golive: GoLive,
        ha_upstream: str,
    ) -> None:
        self._store = store
        self._supervisor = supervisor
        self._session = session
        self._caddy = caddy
        self._frpc = frpc
        self._crowdsec = crowdsec
        self._certwatch = certwatch
        self._guard = guard
        self._golive = golive
        self._ha_upstream = ha_upstream

    def app(self) -> web.Application:
        app = web.Application(middlewares=[ingress_only, friendly_errors])
        r = app.router
        r.add_get("/", self.index)
        r.add_static("/static", STATIC)
        r.add_get("/api/state", self.state)
        r.add_post("/api/step", self.set_step)
        r.add_post("/api/step-done", self.step_done)
        r.add_post("/api/cloudflare", self.cloudflare_token)
        r.add_get("/api/cloudflare/zones", self.cloudflare_zones)
        r.add_post("/api/domain", self.domain)
        r.add_get("/api/hostname/suggest", self.suggest_hostname)
        r.add_post("/api/connection", self.connection)
        r.add_get("/api/relay", self.relay_info)
        r.add_post("/api/relay", self.relay_host)
        r.add_post("/api/relay/new-key", self.relay_new_key)
        r.add_get("/api/hosted", self.hosted_status)
        r.add_post("/api/hosted/start", self.hosted_start)
        r.add_get("/api/ha-settings", self.ha_settings_status)
        r.add_post("/api/ha-settings/apply", self.ha_settings_apply)
        r.add_post("/api/ha-settings/confirm", self.ha_settings_confirm)
        r.add_get("/api/users", self.users)
        r.add_post("/api/users/{user_id}/local-only", self.users_local_only)
        r.add_get("/api/go-live", self.go_live_progress)
        r.add_post("/api/go-live", self.go_live_start)
        r.add_get("/api/router", self.router)
        r.add_post("/api/router/test", self.router_test)
        r.add_post("/api/router/port", self.router_port)
        r.add_post("/api/router/auto", self.router_auto)
        r.add_get("/api/qr.svg", self.qr)
        r.add_get("/api/status", self.status)
        r.add_get("/api/bans", self.bans)
        r.add_post("/api/bans/unban", self.unban)
        r.add_post("/api/settings", self.update_settings)
        return app

    async def index(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    def _state(self, request: web.Request) -> dict:
        settings = self._store.settings
        return {"settings": settings.public(), "user_id": user_id(request)}

    async def _steps(self, request: web.Request) -> dict:
        """The setup steps with their status, plus where the panel should open."""
        settings = self._store.settings
        (http,) = await self._supervisor.ws({"type": "http/config"})
        people = await users.list_people(self._supervisor, user_id(request))
        computed = steps.compute(
            settings,
            http_ok=checks.check_http(http.get("stable") or {}).ok,
            blocking=users.blocking(people),
            people=len(people),
        )
        return {
            **self._state(request),
            "steps": steps.as_dicts(computed),
            "complete": steps.complete(computed),
            "landing": steps.landing(settings, computed),
        }

    async def state(self, request: web.Request) -> web.Response:
        return web.json_response(await self._steps(request))

    async def set_step(self, request: web.Request) -> web.Response:
        step = (await request.json()).get("step")
        if step not in ALL_STEPS:
            raise web.HTTPBadRequest()
        self._store.update(wizard_step=step)
        return web.json_response({"ok": True})

    async def step_done(self, request: web.Request) -> web.Response:
        """Steps only the user can confirm: the port forward works, and they've seen the phone setup."""
        step = (await request.json()).get("step")
        if step == "router":
            self._store.update(router_done=True)
        elif step == "devices":
            self._store.update(devices_done=True)
        else:
            raise web.HTTPBadRequest()
        return web.json_response(await self._steps(request))

    async def connection(self, request: web.Request) -> web.Response:
        mode = (await request.json()).get("mode")
        if mode not in ("direct", "relay", "hosted"):
            raise web.HTTPBadRequest()
        settings = self._store.settings
        changes: dict = {"mode": mode}
        if mode != settings.mode:
            changes["live"] = False  # the address has to point somewhere else now
            changes["router_done"] = False
            if mode in ("relay", "hosted"):
                changes["external_port"] = 443  # the relay has port 443 to itself
        if mode == "relay" and not settings.relay_token:
            changes["relay_token"] = relay.new_token()
        self._store.update(**changes)
        return web.json_response(await self._steps(request))

    async def relay_info(self, _request: web.Request) -> web.Response:
        settings = self._store.settings
        return web.json_response(
            {
                "command": relay.install_command(settings, relay.ensure_pki()),
                "host": settings.relay_host,
                "control_port": settings.relay_control_port,
                "public_port": settings.external_port,
            }
        )

    async def relay_host(self, request: web.Request) -> web.Response:
        host = ((await request.json()).get("host") or "").strip().lower()
        if not relay.valid_relay_host(host):
            return web.json_response({"error": "Enter the server's IP address, for example 203.0.113.25."}, status=400)
        ip = await relay.resolve_ipv4(host)
        if not ip:
            return web.json_response({"error": f"AirBound can't find {host}. Check the address."}, status=400)
        reachable = await network.port_open(ip, self._store.settings.relay_control_port)
        if host != self._store.settings.relay_host:
            self._store.update(relay_host=host, live=False)
        public = ipaddress.ip_address(ip).is_global
        return web.json_response({"host": host, "ip": ip, "reachable": reachable, "public": public})

    async def relay_new_key(self, _request: web.Request) -> web.Response:
        relay.ensure_pki(renew=True)
        self._store.update(relay_token=relay.new_token(), live=False)
        return await self.relay_info(_request)

    async def hosted_start(self, _request: web.Request) -> web.Response:
        """A new link request: the person opens the link and logs in with Patreon."""
        link = await hosted.start_link(self._session, self._store.settings.install_id)
        self._store.update(hosted_claim=link["claim"], hosted_poll=link["poll"], hosted_code=link["code"])
        return await self.hosted_status(_request)

    async def _restore_hosted_address(self) -> None:
        """Back to the hosted relay after another mode: the device is still linked, so
        its address comes back without a new Patreon login."""
        try:
            info = await hosted.account(self._session, self._store.settings.hosted_key)
        except hosted.HostedError:
            return
        fqdn = info.get("fqdn") or ""
        if info.get("state") not in ("active", "grace") or "." not in fqdn:
            return
        hostname, domain = fqdn.split(".", 1)
        settings = self._store.settings
        since = settings.hosted_since or datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        await self._caddy.stop()
        self._store.update(domain=domain, hostname=hostname, tls="hosted", live=False, hosted_since=since)

    async def hosted_status(self, _request: web.Request) -> web.Response:
        """Where linking stands. Picks up the key once the person has logged in."""
        settings = self._store.settings
        if not settings.hosted_claim and settings.hosted_key and settings.tls != "hosted":
            await self._restore_hosted_address()
            settings = self._store.settings
        result = {"linked": bool(settings.hosted_key and settings.tls == "hosted"), "fqdn": settings.fqdn if settings.tls == "hosted" else ""}
        if not settings.hosted_claim:
            return web.json_response({**result, "state": "linked" if result["linked"] else "idle"})
        answer = await hosted.poll_link(self._session, settings.hosted_claim, settings.hosted_poll)
        state = answer.get("state")
        if state == "approved":
            device = answer["device"]
            new_fqdn = f"{device['hostname']}.{device['domain']}"
            if new_fqdn != settings.fqdn or settings.tls != "hosted":
                await self._caddy.stop()
                now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                self._store.update(live=False, hosted_since=now)
            self._store.update(
                hosted_device=device["id"],
                hosted_key=device["key"],
                domain=device["domain"],
                hostname=device["hostname"],
                tls="hosted",
                hosted_claim="",
                hosted_poll="",
                hosted_code="",
            )
            return web.json_response({"linked": True, "fqdn": new_fqdn, "state": "linked"})
        if state in ("expired", "delivered"):
            # "delivered" without our having stored the key means it was lost; start over.
            self._store.update(hosted_claim="", hosted_poll="", hosted_code="")
            return web.json_response({**result, "state": "expired" if not result["linked"] else "linked"})
        return web.json_response(
            {
                **result,
                "state": state,
                "code": settings.hosted_code,
                "link_url": f"{hosted.SERVICE}/link/{settings.hosted_claim}",
                "join_url": answer.get("join_url", ""),
            }
        )

    async def cloudflare_token(self, request: web.Request) -> web.Response:
        token = ((await request.json()).get("token") or "").strip()
        if not token:
            return web.json_response({"error": "Paste the token first."}, status=400)
        cloudflare = Cloudflare(self._session, token)
        await cloudflare.verify()
        zones = await cloudflare.zones()
        self._store.update(cloudflare_token=token)
        return web.json_response({"zones": zones})

    async def cloudflare_zones(self, _request: web.Request) -> web.Response:
        token = self._store.settings.cloudflare_token
        if not token:
            return web.json_response({"zones": []})
        return web.json_response({"zones": await Cloudflare(self._session, token).zones()})

    async def domain(self, request: web.Request) -> web.Response:
        body = await request.json()
        settings = self._store.settings
        # Only a zone the token can see: the name ends up in Caddy's configuration.
        zones = await Cloudflare(self._session, settings.cloudflare_token).zones()
        if not any(z["id"] == body.get("id") and z["name"] == body.get("name") for z in zones):
            return web.json_response({"error": "Pick one of the domains in the list."}, status=400)
        hostname = (body.get("hostname") or "").strip().lower()
        if hostname and not valid_hostname(hostname):
            return web.json_response(
                {"error": "Use only letters a–z, digits and dashes (not at the start or end), up to 63 characters."},
                status=400,
            )
        if not hostname:
            keep = body["name"] == settings.domain and settings.hostname and settings.tls == "cloudflare"
            hostname = settings.hostname if keep else random_hostname()
        new_fqdn = f"{hostname}.{body['name']}"
        if new_fqdn != settings.fqdn or settings.tls != "cloudflare":
            # A new address has to go live again: new DNS record and certificate.
            await self._caddy.stop()
            self._store.update(live=False)
        self._store.update(domain=body["name"], zone_id=body["id"], hostname=hostname, tls="cloudflare")
        return web.json_response({"settings": self._store.settings.public()})

    async def suggest_hostname(self, _request: web.Request) -> web.Response:
        return web.json_response({"hostname": random_hostname(), "common": sorted(COMMON_HOSTNAMES)})

    async def ha_settings_status(self, _request: web.Request) -> web.Response:
        return web.json_response(await ha_settings.status(self._supervisor, self._session, self._ha_upstream))

    async def ha_settings_apply(self, _request: web.Request) -> web.Response:
        await ha_settings.apply(self._supervisor)
        return web.json_response({"ok": True})

    async def ha_settings_confirm(self, _request: web.Request) -> web.Response:
        await ha_settings.confirm(self._supervisor)
        return web.json_response({"ok": True})

    async def users(self, request: web.Request) -> web.Response:
        people = await users.list_people(self._supervisor, user_id(request))
        limited = set(self._store.settings.auto_limited)
        for person in people:
            person["auto_limited"] = person["id"] in limited
        return web.json_response({"users": people, "blocking": users.blocking(people)})

    async def users_local_only(self, request: web.Request) -> web.Response:
        local_only = bool((await request.json()).get("local_only"))
        user = request.match_info["user_id"]
        settings = self._store.settings
        if not local_only and settings.live and settings.require_mfa:
            person = next((p for p in await users.list_people(self._supervisor, None) if p["id"] == user), None)
            if person and not person["mfa"]:
                return web.json_response(
                    {"error": f"{person['name']} needs two-step login before they can use Home Assistant from outside."}, status=400
                )
        await users.set_local_only(self._supervisor, user, local_only)
        if not local_only and user in settings.auto_limited:
            self._store.update(auto_limited=tuple(u for u in settings.auto_limited if u != user))
        return await self.users(request)

    async def go_live_progress(self, _request: web.Request) -> web.Response:
        return web.json_response({**self._golive.progress(), "live": self._store.settings.live})

    async def go_live_start(self, _request: web.Request) -> web.Response:
        self._golive.start()
        return await self.go_live_progress(_request)

    async def router(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ha_ip": await network.ha_ip(self._supervisor),
                "ha_port": int(self._ha_upstream.rsplit(":", 1)[1]),
                "public_ip": await network.public_ip(self._session),
                "fqdn": self._store.settings.fqdn,
            }
        )

    async def router_test(self, _request: web.Request) -> web.Response:
        """Connect to the home's public address from inside, through the router (NAT loopback)."""
        settings = self._store.settings
        ip = await network.public_ip(self._session)
        answer, owner = await network.who_answers(ip, settings.external_port, settings.fqdn) if ip else ("unknown", "")
        return web.json_response(
            {
                "answer": answer,
                "owner": owner,
                "reachable": answer == "airbound",
                "public_ip": ip,
                "port": settings.external_port,
            }
        )

    async def router_auto(self, _request: web.Request) -> web.Response:
        """Ask the router to open the port itself (NAT-PMP or UPnP)."""
        settings = self._store.settings
        gateway = await portmap.gateway_of(self._supervisor)
        ha_ip = await network.ha_ip(self._supervisor)
        if not gateway or not ha_ip:
            return web.json_response({"error": "AirBound couldn't find your router's address."}, status=400)
        try:
            method = await portmap.open_port(self._session, gateway, ha_ip, settings.external_port)
        except portmap.PortMapError:
            return web.json_response({"opened": False, "gateway": gateway})
        self._store.update(port_mapping=method)
        return web.json_response({"opened": True, "method": method, "gateway": gateway})

    async def router_port(self, request: web.Request) -> web.Response:
        try:
            port = int((await request.json()).get("port"))
        except (TypeError, ValueError):
            port = 0
        if not 1 <= port <= 65535:
            return web.json_response({"error": "Use a port number between 1 and 65535, for example 8443."}, status=400)
        if port != self._store.settings.external_port:
            self._store.update(external_port=port, router_done=False)
        return web.json_response({"settings": self._store.settings.public()})

    async def qr(self, _request: web.Request) -> web.Response:
        out = io.BytesIO()
        segno.make(self._store.settings.url, error="m").save(out, kind="svg", scale=6, border=2)
        return web.Response(body=out.getvalue(), content_type="image/svg+xml")

    async def status(self, request: web.Request) -> web.Response:
        results = await checks.run_all(
            self._store.settings, self._supervisor, self._session, self._frpc, self._crowdsec, user_id(request), self._certwatch
        )
        return web.json_response(
            {
                "checks": [c.as_dict() for c in results],
                "refused_logins": self._guard.refused,
                "proxy_running": self._caddy.running,
            }
        )

    async def bans(self, _request: web.Request) -> web.Response:
        """CrowdSec's time-limited blocks and Home Assistant's own permanent ones."""
        blocks = [{**b, "source": "crowdsec"} for b in await self._crowdsec.bans()]
        blocks += [
            {"ip": b["ip"], "reason": "Home Assistant: repeated failed logins", "expires_in": "", "banned_at": b["banned_at"], "source": "homeassistant"}
            for b in ha_bans.read_bans()
        ]
        return web.json_response({"bans": blocks, "running": self._crowdsec.running})

    async def unban(self, request: web.Request) -> web.Response:
        ip = ((await request.json()).get("ip") or "").strip()
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            return web.json_response({"error": "That isn't an IP address."}, status=400)
        await self._crowdsec.unban(ip)
        return await self.bans(request)

    async def update_settings(self, request: web.Request) -> web.Response:
        body = await request.json()
        changes = {}
        if "require_mfa" in body:
            changes["require_mfa"] = bool(body["require_mfa"])
        if body.get("tls") in ("cloudflare", "internal"):  # "internal" is for automated tests only
            changes["tls"] = body["tls"]
        if "domain" in body and "hostname" in body and body.get("tls") == "internal":
            if not (relay.valid_relay_host(body["domain"]) and valid_hostname(body["hostname"])):
                return web.json_response({"error": "Invalid domain or hostname."}, status=400)
            changes.update(domain=body["domain"], hostname=body["hostname"], live=False)
        self._store.update(**changes)
        return web.json_response({"settings": self._store.settings.public()})
