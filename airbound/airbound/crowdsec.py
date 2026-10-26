"""CrowdSec: spots attacks in the logs and blocks the addresses behind them.

The agent reads two logs:
- Caddy's access log, for scanning and probing (crowdsecurity/caddy)
- an auth log the guard writes in Home Assistant's own format, so CrowdSec's
  ready-made Home Assistant brute-force scenario applies (crowdsecurity/home-assistant)

Caddy asks CrowdSec's local API which addresses are banned and refuses them.
Private addresses are never banned (crowdsecurity/whitelists), so nobody at home
can be locked out. The community blocklist adds addresses known to attack others.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import secrets
import shutil
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

BASE = Path("/data/crowdsec")
ETC = BASE / "etc"
DATA = BASE / "data"
CONFIG = ETC / "config.yaml"
BOUNCER_KEY = BASE / "bouncer.key"
DEFAULT_ETC = Path("/etc/crowdsec-default")
DEFAULT_DATA = Path("/var/lib/crowdsec-default")
AUTH_LOG = Path("/data/logs/auth.log")
ACCESS_LOG = Path("/data/logs/access.log")
LAPI = "127.0.0.1:8081"
COLLECTIONS = ("crowdsecurity/caddy", "crowdsecurity/home-assistant")
# syslog-logs also holds "non-syslog", which makes plain log files readable at all.
# whitelists keeps private addresses from ever being banned.
PARSERS = (
    "crowdsecurity/syslog-logs",
    "crowdsecurity/dateparse-enrich",
    "crowdsecurity/geoip-enrich",
    "crowdsecurity/whitelists",
)


def config_yaml() -> str:
    return f"""common:
  log_media: stdout
  log_level: warning
config_paths:
  config_dir: {ETC}/
  data_dir: {DATA}/
  simulation_path: {ETC}/simulation.yaml
  hub_dir: {ETC}/hub/
  index_path: {ETC}/hub/.index.json
  notification_dir: {ETC}/notifications/
  plugin_dir: /usr/local/lib/crowdsec/plugins/
crowdsec_service:
  acquisition_path: {ETC}/acquis.yaml
  acquisition_dir: {ETC}/acquis.d
  parser_routines: 1
cscli:
  output: human
db_config:
  type: sqlite
  db_path: {DATA}/crowdsec.db
  use_wal: true
  flush:
    max_items: 5000
    max_age: 7d
api:
  client:
    insecure_skip_verify: false
    credentials_path: {ETC}/local_api_credentials.yaml
  server:
    log_level: warning
    listen_uri: {LAPI}
    profiles_path: {ETC}/profiles.yaml
    online_client:
      credentials_path: {ETC}/online_api_credentials.yaml
prometheus:
  enabled: false
"""


def acquis_yaml() -> str:
    return f"""filenames:
  - {ACCESS_LOG}
labels:
  type: caddy
---
filenames:
  - {AUTH_LOG}
labels:
  type: home-assistant
"""


def failed_login_line(ip: str, path: str, user_agent: str, now: datetime.datetime | None = None) -> str:
    """A failed login, written exactly like Home Assistant's own log line."""
    now = now or datetime.datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d}"
    agent = (user_agent or "unknown").replace("\n", " ")[:300]
    return (
        f"{stamp} WARNING (MainThread) [homeassistant.components.http.ban] "
        f"Login attempt or request with invalid authentication from {ip} ({ip}). "
        f"Requested URL: '{path}'. ({agent})\n"
    )


def log_failed_login(ip: str, path: str, user_agent: str) -> None:
    AUTH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with AUTH_LOG.open("a") as log:
        log.write(failed_login_line(ip, path, user_agent))


def parse_decisions(raw: str) -> list[dict]:
    """Active local bans from `cscli decisions list -o json`."""
    alerts = json.loads(raw or "null") or []
    bans = []
    for alert in alerts:
        for decision in alert.get("decisions") or []:
            if decision.get("scope", "").lower() != "ip":
                continue
            bans.append(
                {
                    "ip": decision.get("value"),
                    "reason": (alert.get("scenario") or decision.get("scenario") or "").split("/")[-1],
                    "expires_in": decision.get("duration", ""),
                    "id": decision.get("id"),
                }
            )
    return bans


class CrowdSec:
    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self.ready = False

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def bouncer_key(self) -> str:
        return BOUNCER_KEY.read_text().strip() if BOUNCER_KEY.exists() else ""

    async def cscli(self, *args: str, check: bool = True) -> str:
        proc = await asyncio.create_subprocess_exec(
            "cscli", "-c", str(CONFIG), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        if check and proc.returncode != 0:
            raise RuntimeError(f"cscli {' '.join(args[:2])}: {err.decode().strip()[:300]}")
        return out.decode()

    async def setup(self) -> None:
        """First start: config, hub content, machine and bouncer credentials, community blocklist."""
        if not CONFIG.exists():
            # The image's installed hub items are absolute symlinks into its own hub
            # folder; copy only real files and let cscli install the items here.
            shutil.rmtree(ETC, ignore_errors=True)
            shutil.copytree(DEFAULT_ETC, ETC, symlinks=True)
            for link in [p for p in ETC.rglob("*") if p.is_symlink()]:
                link.unlink()
            shutil.copytree(DEFAULT_DATA, DATA, dirs_exist_ok=True)
        (ETC / "acquis.d").mkdir(exist_ok=True)
        CONFIG.write_text(config_yaml())
        (ETC / "acquis.yaml").write_text(acquis_yaml())
        if not (ETC / "simulation.yaml").exists():
            (ETC / "simulation.yaml").write_text("simulation: false\n")
        for log in (ACCESS_LOG, AUTH_LOG):
            log.parent.mkdir(parents=True, exist_ok=True)
            log.touch()

        try:
            await self.cscli("hub", "update")
            await self.cscli("parsers", "install", *PARSERS)
            await self.cscli("collections", "install", *COLLECTIONS)
        except RuntimeError as err:
            _LOGGER.warning("Couldn't update CrowdSec's rules (offline?): %s", err)

        credentials = ETC / "local_api_credentials.yaml"
        if "login:" not in (credentials.read_text() if credentials.exists() else ""):
            credentials.write_text(f"url: http://{LAPI}\n")
            await self.cscli("machines", "add", "--auto", "--force", "-f", str(credentials))
        if not self.bouncer_key:
            key = secrets.token_hex(24)
            await self.cscli("bouncers", "delete", "airbound-caddy", check=False)
            await self.cscli("bouncers", "add", "airbound-caddy", "-k", key)
            BOUNCER_KEY.write_text(key)
            os.chmod(BOUNCER_KEY, 0o600)
        online = ETC / "online_api_credentials.yaml"
        if "login:" not in (online.read_text() if online.exists() else ""):
            try:
                await self.cscli("capi", "register", "-f", str(online))
            except RuntimeError as err:
                _LOGGER.warning("Couldn't join CrowdSec's community blocklist: %s", err)
        self.ready = True

    async def start(self) -> None:
        try:
            await self.setup()
        except Exception:
            _LOGGER.exception("CrowdSec setup failed; AirBound keeps running without it")
            return
        self._process = await asyncio.create_subprocess_exec("crowdsec", "-c", str(CONFIG))
        host, port = LAPI.split(":")
        for _ in range(30):  # let the local API come up before Caddy connects to it
            try:
                _, writer = await asyncio.open_connection(host, int(port))
                writer.close()
                break
            except OSError:
                await asyncio.sleep(0.5)
        _LOGGER.info("CrowdSec started")
        asyncio.get_running_loop().call_later(20, lambda: asyncio.ensure_future(self._log_health()))

    async def _log_health(self) -> None:
        """One line per part, so problems show up in the add-on log."""
        for args in (("capi", "status"), ("lapi", "status")):
            out = await self.cscli(*args, check=False)
            first = next((line.strip() for line in out.splitlines() if line.strip()), "no output")
            _LOGGER.info("CrowdSec %s: %s", args[0], first[:200])
        installed = await self.cscli("scenarios", "list", "-o", "raw", check=False)
        _LOGGER.info("CrowdSec scenarios installed: %d", max(0, len(installed.strip().splitlines()) - 1))
        # Self-test: does CrowdSec read the guard's failed-login lines? (203.0.113.0/24 is a test range.)
        sample = failed_login_line("203.0.113.50", "/auth/login_flow/selftest", "AirBound self-test").strip()
        explained = await self.cscli("explain", "--log", sample, "--type", "home-assistant", check=False)
        parsed = "home-assistant-logs" in explained and "home-assistant-bf" in explained
        _LOGGER.info("CrowdSec self-test: failed logins are %s", "recognised" if parsed else "NOT recognised")
        if not parsed:
            _LOGGER.warning("cscli explain output: %s", explained[-1500:])

    async def stop(self) -> None:
        if self.running:
            assert self._process is not None
            self._process.terminate()
            await self._process.wait()

    async def bans(self) -> list[dict]:
        if not self.ready:
            return []
        return parse_decisions(await self.cscli("decisions", "list", "-o", "json"))

    async def unban(self, ip: str) -> None:
        await self.cscli("decisions", "delete", "--ip", ip)
