import base64
import re

from airbound.caddy import render_caddyfile
from airbound.relay import explain, install_command, new_token, render_frpc
from airbound.settings import Settings, wizard_steps

RELAY = Settings(
    domain="example.com",
    hostname="h",
    cloudflare_token="t",
    mode="relay",
    relay_host="203.0.113.25",
    relay_token="abc123",
)


def test_wizard_steps_per_mode():
    assert "relay" in wizard_steps("relay") and "router" not in wizard_steps("relay")
    assert "router" in wizard_steps("direct") and "relay" not in wizard_steps("direct")
    assert wizard_steps("")[-1] == "done"


def test_install_command_embeds_settings_and_verifies_download():
    command = install_command(RELAY)
    assert command.startswith("echo ") and command.endswith(" | base64 -d | sudo sh")
    script = base64.b64decode(command.split()[1]).decode()
    assert 'TOKEN="abc123"' in script
    assert 'CONTROL_PORT="7000"' in script
    assert 'PUBLIC_PORT="443"' in script
    assert "sha256sum -c" in script
    assert "__" not in script  # every placeholder was filled in


def test_frpc_config_tunnels_to_proxy_protocol_listener():
    config = render_frpc(RELAY)
    assert 'serverAddr = "203.0.113.25"' in config
    assert 'auth.token = "abc123"' in config
    assert "transport.tls.enable = true" in config
    assert "localPort = 8444" in config
    assert "remotePort = 443" in config
    assert 'transport.proxyProtocolVersion = "v2"' in config


def test_caddy_adds_local_relay_listener_only_in_relay_mode():
    relay = render_caddyfile(RELAY, "homeassistant:80")
    assert "proxy_protocol" in relay
    assert "servers 127.0.0.1:8444 {" in relay  # must match the bound listener exactly
    assert "*.example.com:8444 {" in relay
    assert "bind 127.0.0.1" in relay
    direct = render_caddyfile(Settings(domain="example.com", hostname="h", cloudflare_token="t", mode="direct"), "homeassistant:80")
    assert "proxy_protocol" not in direct
    assert "8444" not in direct


def test_relay_token_is_long_and_random():
    assert re.fullmatch(r"[0-9a-f]{48}", new_token())
    assert new_token() != new_token()


def test_public_settings_hide_relay_token():
    assert "relay_token" not in RELAY.public()


def test_explain_errors():
    assert "rejected AirBound's key" in explain("authorization failed: token mismatch")
    assert explain("") == ""
