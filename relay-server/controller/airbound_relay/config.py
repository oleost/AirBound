"""Controller settings, read from /etc/airbound-controller (never from the repository)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("AIRBOUND_CONFIG_DIR", "/etc/airbound-controller"))
STATE_DIR = Path(os.environ.get("AIRBOUND_STATE_DIR", "/var/lib/airbound-controller"))


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"')
    return values


@dataclass(frozen=True)
class Config:
    domain: str = "airbound.dev"
    relay_host: str = "relay.airbound.dev"  # where Home Assistant's frpc connects
    relay_port: int = 7000
    public_ip: str = ""
    campaign_id: str = ""
    tier_id: str = ""  # the Patreon tier that gives access
    patreon_vanity: str = "AirBound"
    client_id: str = ""
    client_secret: str = ""
    creator_access_token: str = ""
    creator_refresh_token: str = ""
    cloudflare_token: str = ""
    site_key: str = ""  # lets the local frpc publish airbound.dev itself
    free_users: frozenset[str] = field(default_factory=frozenset)  # Patreon user ids with free access
    max_devices: int = 2  # Home Assistants per membership
    bandwidth: str = "4MB"  # per Home Assistant, each direction (frp syntax)
    grace_days: int = 3  # access after a membership lapses, to fix a failed payment
    device_monthly_gb: int = 300  # fair use per Home Assistant and month (in + out)
    throttled_bandwidth: str = "1MB"  # past that, for the rest of the month
    server_monthly_gb: int = 15000  # what the server may send a month before everyone slows down
    emergency_bandwidth: str = "512KB"  # everyone, past the server's allowance
    alert_url: str = ""  # optional ntfy-style URL for the operator's alerts
    web_port: int = 7500  # public pages and API, behind Caddy
    plugin_port: int = 7600  # frps server plugin, localhost only
    hub_admin_port: int = 7400  # frps admin API, localhost only, for traffic counters
    hub_admin_password: str = ""
    github_client_id: str = ""  # GitHub OAuth app, for "Log in with GitHub"
    github_client_secret: str = ""
    github_token: str = ""  # the maintainer's token (read:user), to read the sponsor list
    github_webhook_secret: str = ""
    github_sponsorable: str = ""  # the GitHub account people sponsor, e.g. oleost
    github_min_cents: int = 100  # monthly sponsorship that gives access (GitHub tiers are whole dollars)
    ops_token: str = ""  # lets the weekly maintenance report read GET /api/v1/ops

    @property
    def base_url(self) -> str:
        return f"https://{self.domain}"

    @property
    def redirect_uri(self) -> str:
        return f"{self.base_url}/patreon/callback"

    @property
    def join_url(self) -> str:
        """Patreon checkout for the relay tier, which starts the free trial."""
        return f"https://www.patreon.com/checkout/{self.patreon_vanity}?rid={self.tier_id}"

    @property
    def manage_url(self) -> str:
        return f"https://www.patreon.com/c/{self.patreon_vanity}/membership"

    @property
    def github_redirect_uri(self) -> str:
        return f"{self.base_url}/github/callback"

    @property
    def github_sponsor_url(self) -> str:
        return f"https://github.com/sponsors/{self.github_sponsorable}"

    def join_url_for(self, account: str) -> str:
        """Where this account starts or renews the support that gives access."""
        return self.github_sponsor_url if account.startswith("gh:") else self.join_url

    def manage_url_for(self, account: str) -> str:
        return self.github_sponsor_url if account.startswith("gh:") else self.manage_url


def load(config_dir: Path = CONFIG_DIR) -> Config:
    env = {
        **read_env_file(config_dir / "controller.env"),
        **read_env_file(config_dir / "patreon.env"),
        **read_env_file(config_dir / "github.env"),
    }
    token_file = config_dir / "cloudflare_token"
    free = {u.strip() for u in env.get("FREE_PATREON_USERS", "").split(",") if u.strip()}
    return Config(
        domain=env.get("DOMAIN", "airbound.dev"),
        relay_host=env.get("RELAY_HOST", "relay." + env.get("DOMAIN", "airbound.dev")),
        relay_port=int(env.get("RELAY_PORT", "7000")),
        public_ip=env.get("PUBLIC_IP", ""),
        campaign_id=env.get("CAMPAIGN_ID", ""),
        tier_id=env.get("RELAY_TIER_ID", ""),
        patreon_vanity=env.get("PATREON_VANITY", "AirBound"),
        client_id=env.get("PATREON_CLIENT_ID", ""),
        client_secret=env.get("PATREON_CLIENT_SECRET", ""),
        creator_access_token=env.get("PATREON_CREATOR_ACCESS_TOKEN", ""),
        creator_refresh_token=env.get("PATREON_CREATOR_REFRESH_TOKEN", ""),
        cloudflare_token=token_file.read_text().strip() if token_file.exists() else "",
        site_key=env.get("SITE_KEY", ""),
        free_users=frozenset(free),
        max_devices=int(env.get("MAX_DEVICES", "2")),
        bandwidth=env.get("BANDWIDTH", "4MB"),
        grace_days=int(env.get("GRACE_DAYS", "3")),
        device_monthly_gb=int(env.get("DEVICE_MONTHLY_GB", "300")),
        throttled_bandwidth=env.get("THROTTLED_BANDWIDTH", "1MB"),
        server_monthly_gb=int(env.get("SERVER_MONTHLY_GB", "15000")),
        emergency_bandwidth=env.get("EMERGENCY_BANDWIDTH", "512KB"),
        alert_url=env.get("ALERT_URL", ""),
        hub_admin_password=env.get("HUB_ADMIN_PASSWORD", ""),
        github_client_id=env.get("GITHUB_CLIENT_ID", ""),
        github_client_secret=env.get("GITHUB_CLIENT_SECRET", ""),
        github_token=env.get("GITHUB_TOKEN", ""),
        github_webhook_secret=env.get("GITHUB_WEBHOOK_SECRET", ""),
        github_sponsorable=env.get("GITHUB_SPONSORABLE", ""),
        github_min_cents=int(env.get("GITHUB_MIN_CENTS", "100")),
        ops_token=env.get("OPS_TOKEN", ""),
    )
