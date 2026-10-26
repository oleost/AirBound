import datetime
import json
import re

from airbound.caddy import render_caddyfile
from airbound.crowdsec import acquis_yaml, failed_login_line, parse_decisions
from airbound.settings import Settings

# The grok pattern of crowdsecurity/home-assistant-logs, written as a regex.
HA_FAILED_AUTH = re.compile(
    r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} WARNING \((?P<thread>[^)]*)\) "
    r"\[homeassistant\.components\.http\.ban\] Login attempt or request with invalid authentication "
    r"from (?P<rdns>\S+) \((?P<ip>[0-9a-fA-F.:]+)\)\. Requested URL: '(?P<url>[^']*)'\. \((?P<ua>.*)\)$"
)


def test_failed_login_line_matches_home_assistant_format():
    line = failed_login_line("203.0.113.7", "/auth/login_flow/abc", "Mozilla/5.0", datetime.datetime(2026, 10, 26, 16, 8, 1, 711000))
    assert line.endswith("\n")
    match = HA_FAILED_AUTH.match(line.rstrip("\n"))
    assert match, line
    assert match["ip"] == "203.0.113.7"
    assert line.startswith("2026-10-26 16:08:01.711 WARNING (MainThread)")


def test_failed_login_line_survives_hostile_user_agent():
    line = failed_login_line("203.0.113.7", "/x", "evil\nsecond line" + "A" * 1000)
    assert line.count("\n") == 1
    assert len(line) < 600


def test_acquisition_reads_both_logs():
    config = acquis_yaml()
    assert "type: caddy" in config and "type: home-assistant" in config


def test_parse_decisions():
    raw = json.dumps(
        [
            {
                "scenario": "crowdsecurity/home-assistant-bf",
                "decisions": [{"id": 7, "scope": "Ip", "value": "203.0.113.7", "duration": "3h59m", "type": "ban"}],
            },
            {"scenario": "x", "decisions": [{"scope": "Range", "value": "198.51.100.0/24"}]},
        ]
    )
    assert parse_decisions(raw) == [{"ip": "203.0.113.7", "reason": "home-assistant-bf", "expires_in": "3h59m", "id": 7}]
    assert parse_decisions("null") == []


def test_caddy_bouncer_only_with_key_and_runs_first():
    settings = Settings(domain="example.com", hostname="h", cloudflare_token="t", mode="direct")
    with_bouncer = render_caddyfile(settings, "homeassistant:80", crowdsec=True)
    assert "api_key {$CROWDSEC_API_KEY}" in with_bouncer
    route = with_bouncer.split("route {")[1]
    assert route.strip().startswith("crowdsec")
    assert "crowdsec" not in render_caddyfile(settings, "homeassistant:80")
