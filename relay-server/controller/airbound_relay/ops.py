"""The relay's health for the weekly maintenance report (GET /api/v1/ops, operator token only).

Everything here is read from the system without privileges: update stamps,
the reboot flag, disk, memory, uptime, the installed frp build and the
website's certificate.
"""

from __future__ import annotations

import datetime
import platform
import shutil
import socket
import ssl
import time
from pathlib import Path

from .config import Config
from .db import Database
from .limits import health_problems, month_of

APT_STAMPS = Path("/var/lib/apt/periodic")
FRP_BUILD = Path("/usr/local/lib/airbound-frp-build")
CONTROLLER_BUILD = Path(__file__).resolve().parent.parent / "BUILD"
GB = 1000**3


def _age_days(path: Path) -> float | None:
    try:
        return round((time.time() - path.stat().st_mtime) / 86400, 1)
    except OSError:
        return None


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def certificate_days_left(host: str, server_name: str, port: int = 443) -> float | None:
    """Days until the certificate served for server_name expires (verified, so a broken chain shows as None)."""
    try:
        with socket.create_connection((host, port), timeout=5) as raw:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=server_name) as tls:
                not_after = tls.getpeercert()["notAfter"]
    except (OSError, ssl.SSLError, KeyError):
        return None
    expires = datetime.datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=datetime.UTC)
    return round((expires - datetime.datetime.now(datetime.UTC)).total_seconds() / 86400, 1)


def report(config: Config, db: Database, certificate_days: float | None = None, now: float | None = None) -> dict:
    """certificate_days comes from certificate_days_left, which blocks, so the caller runs it off the event loop."""
    now = now or time.time()
    disk = shutil.disk_usage("/")
    try:
        uptime_days = round(float(Path("/proc/uptime").read_text().split()[0]) / 86400, 1)
    except (OSError, ValueError):
        uptime_days = None
    return {
        "time": datetime.datetime.fromtimestamp(now, datetime.UTC).isoformat(timespec="seconds"),
        "os": _read(Path("/etc/debian_version")) or platform.platform(),
        "kernel": platform.release(),
        "uptime_days": uptime_days,
        "reboot_required": Path("/run/reboot-required").exists(),
        "days_since_package_lists_updated": _age_days(APT_STAMPS / "update-stamp"),  # Debian 13 writes no update-success-stamp
        "days_since_unattended_upgrade": _age_days(APT_STAMPS / "upgrade-stamp"),
        "disk_used_percent": round(100 * disk.used / disk.total, 1) if disk.total else None,
        "problems": health_problems(),
        "frp_build": _read(FRP_BUILD),
        "controller_build": _read(CONTROLLER_BUILD),
        "website_certificate_days_left": certificate_days,
        "devices": db.count_devices(),
        "accounts_with_access": db.count_entitled(),
        "server_traffic_gb_this_month": round(db.server_traffic(month_of(now)) / GB, 1),
        "server_allowance_gb": config.server_monthly_gb,
    }
