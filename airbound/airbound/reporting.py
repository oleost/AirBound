"""Tells Home Assistant what AirBound sees: sensors, events and notifications.

- Sensors: through MQTT discovery when a broker is available, so AirBound is a
  device with real entities; otherwise as plain states through HA's REST API.
- Events (airbound_login, airbound_login_refused, airbound_login_failed) for
  automations, plus MQTT device triggers when MQTT is used.
- Notifications for a login from a new address, a certificate about to expire,
  and anything on the status page that turns red.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
from pathlib import Path

from aiohttp import ClientSession
from cryptography import x509

from . import checks, mqtt, network
from .certwatch import CertWatch, own_public_keys
from .crowdsec import CrowdSec
from .relay import Frpc
from .settings import Store
from .supervisor import Supervisor, SupervisorError

_LOGGER = logging.getLogger(__name__)

STATE_FILE = Path("/data/reporting.json")
STATUS_INTERVAL = 300
CONNECTION_INTERVAL = 60
ACCESS_LOG = Path("/data/logs/access.log")
CT_INTERVAL = 6 * 3600
CERT_WARNING_DAYS = 14
PREFIX = "airbound"


def remote_login_message(username: str, ip: str) -> str:
    who = username or "Someone"
    return (
        f"{who} logged in from outside for the first time from {ip}. "
        "If that wasn't you or someone you know, change that user's password "
        "and remove their sessions under Profile → Security."
    )


class Reporter:
    def __init__(
        self, store: Store, supervisor: Supervisor, session: ClientSession, frpc: Frpc, crowdsec: CrowdSec | None = None,
        certwatch: CertWatch | None = None,
        broker: mqtt.Mqtt | None = None,
    ) -> None:
        self._store = store
        self._supervisor = supervisor
        self._session = session
        self._frpc = frpc
        self._crowdsec = crowdsec
        self._certwatch = certwatch
        self._mqtt = broker
        self._state = self._load()
        self._tasks: set[asyncio.Task] = set()
        self._problems: list[str] = []

    # ---------- persistent counters and known addresses ----------

    def _load(self) -> dict:
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text())
            except ValueError:
                pass
        return {"known": {}, "refused": 0, "failed": 0, "last_login": None}

    def _save(self) -> None:
        STATE_FILE.write_text(json.dumps(self._state))

    def remember(self, username: str, ip: str) -> bool:
        """Record a login; True if this user hasn't logged in from this address before."""
        known = self._state["known"].setdefault(username or "?", [])
        if ip in known:
            return False
        known.append(ip)
        del known[:-50]  # keep the 50 most recent addresses per user
        return True

    # ---------- guard events ----------

    def on_guard_event(self, event: str, info: dict) -> None:
        """Called by the auth guard; the work runs in the background."""
        task = asyncio.create_task(self._handle(event, info))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle(self, event: str, info: dict) -> None:
        username, ip = info.get("username", ""), info.get("ip", "")
        try:
            if event == "login":
                new = self.remember(username, ip)
                self._state["last_login"] = {
                    "at": datetime.datetime.now(datetime.UTC).isoformat(),
                    "username": username,
                    "ip": ip,
                }
                self._save()
                await self._event("login", {"username": username, "ip": ip, "new_address": new})
                if new:
                    await self._trigger("login_new_address", {"username": username, "ip": ip})
                if new:
                    await self._notify("New remote login", remote_login_message(username, ip), f"login_{ip}")
            elif event == "refused":
                self._state["refused"] += 1
                self._save()
                await self._event("login_refused", {"username": username, "ip": ip})
            elif event == "failed":
                self._state["failed"] += 1
                self._save()
                await self._event("login_failed", {"username": username, "ip": ip})
            await self._publish_counters()
        except SupervisorError as err:
            _LOGGER.warning("Couldn't report to Home Assistant: %s", err)

    # ---------- talking to Home Assistant ----------

    async def _event(self, name: str, data: dict) -> None:
        await self._supervisor.core_post(f"/events/{PREFIX}_{name}", data)
        await self._trigger(name, data)

    async def _trigger(self, key: str, data: dict) -> None:
        if self._mqtt and key in mqtt.TRIGGERS:
            await self._mqtt.trigger(key, data)

    async def _notify(self, title: str, message: str, notification_id: str) -> None:
        await self._supervisor.core_post(
            "/services/persistent_notification/create",
            {"title": f"AirBound: {title}", "message": message, "notification_id": f"{PREFIX}_{notification_id}"},
        )

    async def _dismiss(self, notification_id: str) -> None:
        await self._supervisor.core_post(
            "/services/persistent_notification/dismiss", {"notification_id": f"{PREFIX}_{notification_id}"}
        )

    async def _set(self, key: str, state, **attributes) -> None:
        """One sensor, by MQTT when connected, else as a plain REST state."""
        if self._mqtt and self._mqtt.connected:
            await self._mqtt.set(key, state, attributes)
            return
        component, name, extra = mqtt.ENTITIES[key]
        await self._supervisor.core_post(
            f"/states/{mqtt.entity_id(key)}",
            {"state": "unknown" if state is None else state, "attributes": {"friendly_name": f"AirBound {name.lower()}", **extra, **attributes}},
        )

    async def _publish_counters(self) -> None:
        await self._set("refused_logins", self._state["refused"])
        await self._set("failed_logins", self._state["failed"])
        last = self._state.get("last_login")
        await self._set(
            "last_remote_login", last["at"] if last else None, username=(last or {}).get("username"), ip=(last or {}).get("ip")
        )
        conn = self._state.get("last_connection")
        await self._set("last_remote_connection", conn["at"] if conn else None, ip=(conn or {}).get("ip"))

    async def certificate_expiry(self) -> datetime.datetime | None:
        settings = self._store.settings
        if not settings.fqdn:
            return None
        der = await network.peer_certificate("127.0.0.1", 443, settings.fqdn)
        if not der:
            return None
        return x509.load_der_x509_certificate(der).not_valid_after_utc

    async def publish_status(self) -> None:
        settings = self._store.settings
        results = await checks.run_all(
            settings, self._supervisor, self._session, self._frpc, self._crowdsec, None, self._certwatch
        )
        problems = [c for c in results if not c.ok]
        live = settings.live
        status = "Off" if not live else ("OK" if not problems else "Needs attention")
        await self._set("remote_access", "on" if live and not problems else "off", url=settings.url or None, mode=settings.mode or None)
        await self._set("status", status, problems=[f"{c.title}: {c.detail}" for c in problems])
        expiry = await self.certificate_expiry() if live else None
        await self._set("certificate_expires", expiry.isoformat() if expiry else None)
        bans = await self._crowdsec.bans() if self._crowdsec else []
        await self._set("blocked_addresses", len(bans), addresses=[b["ip"] for b in bans][:50])
        await self._publish_counters()

        # Notify once when something turns red, and clear it when it's fixed.
        titles = sorted(c.title for c in problems) if live else []
        if titles != self._problems:
            if titles:
                lines = "\n".join(f"- **{c.title}**: {c.detail}" for c in problems)
                await self._notify("Needs attention", f"{lines}\n\nOpen AirBound in the sidebar to fix it.", "status")
            else:
                await self._dismiss("status")
            self._problems = titles
        if expiry and expiry - datetime.datetime.now(datetime.UTC) < datetime.timedelta(days=CERT_WARNING_DAYS):
            await self._notify(
                "Certificate expires soon",
                f"The certificate for {settings.fqdn} expires on {expiry:%d %B %Y}. AirBound normally renews it by "
                "itself 30 days before; check that the Cloudflare token still works.",
                "certificate",
            )

    async def watch_certificates(self) -> bool:
        """Scan the CT logs; returns whether a scan actually ran."""
        settings = self._store.settings
        if not (self._certwatch and settings.live and settings.tls in ("cloudflare", "hosted")):
            return False
        own_keys = None
        if settings.tls == "hosted":
            # Keys of every certificate this Home Assistant got, including renewed-away ones.
            known_keys = set(self._state.setdefault("own_keys", [])) | own_public_keys(settings.fqdn)
            self._state["own_keys"] = sorted(known_keys)
            self._save()
            if not known_keys:
                return False  # nothing to compare against before the first certificate
            own_keys = known_keys
        since = settings.hosted_since if settings.tls == "hosted" else ""
        found = await self._certwatch.scan(self._session, settings.domain, settings.fqdn, own_keys, since)
        if found is None:
            return False
        known = set(self._state.setdefault("reported_certs", []))
        new = [c for c in found or [] if c["id"] not in known]
        if new:
            lines = "\n".join(f"- {c['issuer']}, valid from {c['not_before']}: {c['url']}" for c in new)
            await self._notify(
                "Unexpected certificate",
                f"Certificate Transparency logs show a certificate for {settings.fqdn} that AirBound didn't order:\n"
                f"{lines}\n\nSomeone may be trying to intercept your traffic. Turn off remote access until you know why.",
                "unexpected_certificate",
            )
            self._state["reported_certs"] = sorted(known | {c["id"] for c in new})
            self._save()
        return True

    async def track_connections(self) -> None:
        """Update "Last remote connection" from Caddy's access log."""
        settings = self._store.settings
        if not (settings.live and ACCESS_LOG.exists()):
            return
        with ACCESS_LOG.open("rb") as log:
            log.seek(0, 2)
            log.seek(max(0, log.tell() - 65536))
            tail = log.read().decode(errors="replace").splitlines()
        found = last_connection(tail, settings.fqdn)
        previous = self._state.get("last_connection") or {}
        if found and found["at"] != previous.get("at"):
            self._state["last_connection"] = found
            self._save()
            await self._set("last_remote_connection", found["at"], ip=found["ip"])

    async def run(self) -> None:
        await asyncio.sleep(10)  # let the proxy start first
        last_ct_scan = last_status = None
        while True:
            now = asyncio.get_running_loop().time()
            if last_ct_scan is None or now - last_ct_scan > CT_INTERVAL:
                try:
                    if await self.watch_certificates():
                        last_ct_scan = now
                except Exception:
                    _LOGGER.exception("Certificate Transparency check failed")
            if last_status is None or now - last_status >= STATUS_INTERVAL:
                try:
                    await self.publish_status()
                    last_status = now
                except Exception:
                    _LOGGER.exception("Couldn't publish status to Home Assistant")
            try:
                await self.track_connections()
            except Exception:
                _LOGGER.exception("Couldn't read the access log")
            await asyncio.sleep(CONNECTION_INTERVAL)


def last_connection(lines: list[str], fqdn: str) -> dict | None:
    """The newest request for the hostname in Caddy's JSON access log: {"at", "ip"}."""
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        request = entry.get("request") or {}
        host = (request.get("host") or "").split(":")[0].lower()
        if host != fqdn.lower():
            continue
        ip = request.get("client_ip") or request.get("remote_ip") or ""
        at = datetime.datetime.fromtimestamp(float(entry.get("ts", 0)), datetime.UTC).isoformat()
        return {"at": at, "ip": ip}
    return None
