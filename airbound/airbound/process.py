"""The environment AirBound's helper programs (Caddy, frpc, CrowdSec) start with.

They face the internet, and the add-on's own environment holds the Supervisor
token, which is as good as an administrator's login to Home Assistant. They
never need it, so they only get what a program needs to run.
"""

from __future__ import annotations

import os

KEEP = (
    "PATH", "HOME", "TZ", "LANG", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
)


def child_env(**extra: str) -> dict[str, str]:
    """A small environment for a child process, plus what that process itself needs."""
    return {**{name: os.environ[name] for name in KEEP if name in os.environ}, **extra}
