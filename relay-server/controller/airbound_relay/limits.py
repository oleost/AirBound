"""Fair use: monthly traffic per Home Assistant and for the whole server.

Every few minutes the controller reads each tunnel's traffic from the hub's
local admin API and adds it to this month's total. Past the per-device
allowance a Home Assistant gets a lower speed for the rest of the month; past
the server's allowance everyone does, and the operator is alerted. frps sets a
tunnel's speed when it's created, so a change takes effect when the tunnel
reconnects, which the next heartbeat forces (see frp.Plugin).

Only the amount of data is kept, per device and month, for three months.
"""

from __future__ import annotations

import datetime
import logging
import os
import time
from pathlib import Path

from aiohttp import BasicAuth, ClientError, ClientSession, ClientTimeout

from .config import Config
from .db import Database

_LOGGER = logging.getLogger(__name__)

GB = 1000**3
TIMEOUT = ClientTimeout(total=10)
KEEP_MONTHS = 3
SERVER_WARN = 0.8  # alert at this share of the server's monthly allowance


def month_of(now: float) -> str:
    return datetime.datetime.fromtimestamp(now, datetime.UTC).strftime("%Y-%m")


def months_ago(now: float, count: int) -> str:
    day = datetime.datetime.fromtimestamp(now, datetime.UTC).date().replace(day=1)
    for _ in range(count):
        day = (day - datetime.timedelta(days=1)).replace(day=1)
    return day.strftime("%Y-%m")


def device_of(proxy_name: str) -> str:
    """frps names a proxy "<user>.<name>"; for a Home Assistant the user is its device id."""
    return proxy_name.split(".", 1)[0]


class Limits:
    def __init__(self, config: Config, db: Database) -> None:
        self._config = config
        self._db = db
        self._last_proxy: dict[str, int] = {}  # traffic counters at the last reading
        self._last_tx: int | None = None
        self._given: dict[str, str] = {}  # the speed each device's tunnel was created with

    # Speeds

    def server_over(self, now: float | None = None) -> bool:
        return self._db.server_traffic(month_of(now or time.time())) >= self._config.server_monthly_gb * GB

    def device_over(self, device_id: str, now: float | None = None) -> bool:
        return self._db.traffic(month_of(now or time.time()), device_id) >= self._config.device_monthly_gb * GB

    def bandwidth_for(self, device_id: str, now: float | None = None) -> str:
        if self.server_over(now):
            return self._config.emergency_bandwidth
        if self.device_over(device_id, now):
            return self._config.throttled_bandwidth
        return self._config.bandwidth

    def given(self, device_id: str, bandwidth: str) -> None:
        self._given[device_id] = bandwidth

    def needs_reconnect(self, device_id: str, now: float | None = None) -> bool:
        """True when this tunnel runs at another speed than it should now."""
        target = self.bandwidth_for(device_id, now)
        if device_id not in self._given:
            # Started before the controller did: only a lower speed is worth a reconnect.
            self._given[device_id] = self._config.bandwidth
        return self._given[device_id] != target

    # Metering

    def record(self, proxies: dict[str, int], tx_bytes: int | None, now: float | None = None) -> list[str]:
        """Add the traffic since the last reading; returns alerts for allowances crossed just now.

        proxies maps each proxy's name to its traffic today (in + out), as frps
        counts it. A counter that went down means a new day or a restarted hub,
        so all of it is new.
        """
        now = now or time.time()
        month = month_of(now)
        server_was_over = self.server_over(now)
        server_was_warn = self._db.server_traffic(month) >= SERVER_WARN * self._config.server_monthly_gb * GB
        alerts = []
        for name, total in proxies.items():
            last = self._last_proxy.get(name)
            self._last_proxy[name] = total
            if last is None:
                continue  # first reading after a restart: only a baseline
            delta = total - last if total >= last else total
            device = self._db.device(device_of(name))
            if delta <= 0 or device is None:
                continue
            was_over = self.device_over(device.id, now)
            self._db.add_traffic(month, device.id, delta)
            if not was_over and self.device_over(device.id, now):
                alerts.append(
                    f"{device.hostname}.{self._config.domain} passed {self._config.device_monthly_gb} GB this month "
                    f"and now runs at {self._config.throttled_bandwidth}/s until the month ends."
                )
        if tx_bytes is not None:
            if self._last_tx is not None:
                delta = tx_bytes - self._last_tx if tx_bytes >= self._last_tx else tx_bytes
                self._db.add_server_traffic(month, delta)
            self._last_tx = tx_bytes
        used = self._db.server_traffic(month) / GB
        if not server_was_over and self.server_over(now):
            alerts.append(
                f"The server sent {used:.0f} GB this month, past its allowance of {self._config.server_monthly_gb} GB. "
                f"Every Home Assistant now runs at {self._config.emergency_bandwidth}/s until the month ends."
            )
        elif not server_was_warn and used >= SERVER_WARN * self._config.server_monthly_gb:
            alerts.append(f"The server sent {used:.0f} GB this month, {used / self._config.server_monthly_gb:.0%} of its allowance.")
        self._db.forget_traffic_before(months_ago(now, KEEP_MONTHS - 1))
        return alerts


async def hub_traffic(session: ClientSession, config: Config) -> dict[str, int]:
    """Each HTTPS proxy's traffic today, from the hub's admin API on localhost."""
    url = f"http://127.0.0.1:{config.hub_admin_port}/api/proxy/https"
    async with session.get(url, auth=BasicAuth("controller", config.hub_admin_password), timeout=TIMEOUT) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    return {
        p["name"]: int(p.get("todayTrafficIn") or 0) + int(p.get("todayTrafficOut") or 0)
        for p in data.get("proxies") or []
        if p.get("name")
    }


def default_interface() -> str | None:
    """The interface of the default route, from /proc/net/route."""
    try:
        for line in Path("/proc/net/route").read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) > 2 and fields[1] == "00000000":
                return fields[0]
    except OSError:
        pass
    return None


def sent_bytes(interface: str | None) -> int | None:
    """Bytes the server has sent since boot; providers bill outgoing traffic."""
    if not interface:
        return None
    try:
        return int(Path(f"/sys/class/net/{interface}/statistics/tx_bytes").read_text())
    except (OSError, ValueError):
        return None


def health_problems(memory_floor_mb: int = 100, disk_floor: float = 0.1) -> list[str]:
    """What looks strained on the server right now."""
    problems = []
    cpus = os.cpu_count() or 1
    load = os.getloadavg()[2]
    if load > 0.9 * cpus:
        problems.append(f"The server is busy: average load {load:.2f} over 15 minutes on {cpus} processor(s).")
    try:
        meminfo = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line)
        available = int(meminfo["MemAvailable"].split()[0]) // 1024
        if available < memory_floor_mb:
            problems.append(f"The server is low on memory: {available} MB available.")
    except (OSError, KeyError, ValueError):
        pass
    disk = os.statvfs("/")
    if disk.f_blocks and disk.f_bavail / disk.f_blocks < disk_floor:
        problems.append(f"The server's disk is filling up: {1 - disk.f_bavail / disk.f_blocks:.0%} full.")
    return problems


async def alert(session: ClientSession, config: Config, message: str) -> None:
    """To the operator: an ntfy-style URL if one is set (ALERT_URL), and always the journal."""
    _LOGGER.warning("Alert: %s", message)
    if not config.alert_url:
        return
    try:
        async with session.post(config.alert_url, data=message.encode(), headers={"Title": f"AirBound relay ({config.domain})"}, timeout=TIMEOUT) as resp:
            if resp.status >= 300:
                _LOGGER.warning("Alert URL answered %s", resp.status)
    except (ClientError, TimeoutError) as err:
        _LOGGER.warning("Couldn't send the alert: %s", err)
