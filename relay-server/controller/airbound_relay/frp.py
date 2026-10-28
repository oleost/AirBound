"""frps server plugin: every login, new proxy and heartbeat is checked here.

A Home Assistant's frpc sends its device key and its machine as metadata. It
may only log in from the machine that claimed the key (db.on_machine), while
its membership gives access, may only publish an HTTPS proxy for its own
hostname, and is disconnected at the next heartbeat once access ends. The
bandwidth limit is set here, so a client can't raise its own; when it should
change (fair use, see limits.py), the next heartbeat makes the tunnel reconnect.
"""

from __future__ import annotations

import time

from . import access
from .config import Config
from .db import Database, Device
from .limits import Limits

SEEN_EVERY = 300  # write last_seen at most every 5 minutes per device


class Plugin:
    def __init__(self, config: Config, db: Database, limits: Limits | None = None) -> None:
        self._config = config
        self._db = db
        self._limits = limits or Limits(config, db)
        self._seen: dict[str, float] = {}

    def handle(self, op: str, content: dict, now: float | None = None) -> dict:
        now = now or time.time()
        if op == "Login":
            return self._login(content, now)
        if op == "NewProxy":
            return self._new_proxy(content, now)
        if op == "Ping":
            return self._ping(content, now)
        return allow()

    def _identify(self, user: str, metas: dict | None, now: float) -> tuple[Device | None, str]:
        """(device, "") if this client may be connected, else (None, reason)."""
        key = (metas or {}).get("key", "")
        if self._config.site_key and key == self._config.site_key and user == "site":
            return None, ""
        device = self._db.device_by_key(key) if key else None
        if device is None or device.id != user:
            return None, "unknown device: link this Home Assistant again in AirBound"
        if not self._db.on_machine(device, (metas or {}).get("machine", ""), now):
            return None, "other machine: this key belongs to another Home Assistant; link this one again in AirBound"
        account = self._db.account(device.patreon_id)
        if access.state(account, device.patreon_id, self._config.free_users, self._config.grace_days, now) == access.EXPIRED:
            return None, "membership ended: renew it on Patreon to use the hosted relay"
        return device, ""

    def _login(self, content: dict, now: float) -> dict:
        _device, reason = self._identify(content.get("user", ""), content.get("metas"), now)
        return reject(reason) if reason else allow()

    def _new_proxy(self, content: dict, now: float) -> dict:
        user = content.get("user") or {}
        device, reason = self._identify(user.get("user", ""), user.get("metas"), now)
        if reason:
            return reject(reason)
        hostname = f"{device.hostname}.{self._config.domain}" if device else self._config.domain
        if content.get("proxy_type") != "https":
            return reject("only HTTPS proxies are allowed")
        if content.get("custom_domains") != [hostname] or content.get("subdomain"):
            return reject(f"this device may only publish {hostname}")
        if device is None:  # the site itself is not rate limited
            return allow()
        bandwidth = self._limits.bandwidth_for(device.id, now)
        self._limits.given(device.id, bandwidth)
        return {
            "reject": False,
            "unchange": False,
            "content": {**content, "bandwidth_limit": bandwidth, "bandwidth_limit_mode": "server"},
        }

    def _ping(self, content: dict, now: float) -> dict:
        user = content.get("user") or {}
        device, reason = self._identify(user.get("user", ""), user.get("metas"), now)
        if reason:
            return reject(reason)
        if device and now - self._seen.get(device.id, 0) > SEEN_EVERY:
            self._seen[device.id] = now
            self._db.seen(device.id, now)
        if device and self._limits.needs_reconnect(device.id, now):
            return reject("fair-use speed changed: reconnecting")
        return allow()


def allow() -> dict:
    return {"reject": False, "unchange": True}


def reject(reason: str) -> dict:
    return {"reject": True, "reject_reason": reason}
