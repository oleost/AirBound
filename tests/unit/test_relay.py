import base64
import gzip
import re

from airbound.caddy import render_caddyfile
from cryptography import x509

from airbound.relay import SERVER_NAME, explain, install_command, make_pki, new_token, render_frpc, valid_relay_host
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
    pki = make_pki()
    command = install_command(RELAY, pki)
    assert command.startswith("echo ") and command.endswith(" | base64 -d | gunzip | $(command -v sudo) sh")
    script = gzip.decompress(base64.b64decode(command.split()[1])).decode()
    assert 'TOKEN="abc123"' in script
    assert 'CONTROL_PORT="7000"' in script
    assert 'PUBLIC_PORT="443"' in script
    assert "sha256sum -c" in script
    assert pki["server.crt"].strip() in script and pki["server.key"].strip() in script
    assert "transport.tls.certFile" in script
    assert "__" not in script  # every placeholder was filled in
    assert "harden_firewall()" in script and 'harden_server "$CONTROL_PORT $PUBLIC_PORT"' in script


def test_frpc_config_tunnels_to_proxy_protocol_listener():
    config = render_frpc(RELAY)
    assert 'serverAddr = "203.0.113.25"' in config
    assert 'auth.token = "abc123"' in config
    assert "transport.tls.enable = true" in config
    assert 'transport.tls.trustedCaFile = "/data/relay/ca.crt"' in config  # only our own relay
    assert f'transport.tls.serverName = "{SERVER_NAME}"' in config
    assert "localPort = 8444" in config
    assert "remotePort = 443" in config
    assert 'transport.proxyProtocolVersion = "v2"' in config


def test_caddy_adds_local_relay_listener_only_in_relay_mode():
    relay = render_caddyfile(RELAY, "homeassistant:80")
    assert "proxy_protocol" in relay
    assert "servers :8444 {" in relay  # must match the listener address exactly
    assert relay.count("acme_dns cloudflare") == 1  # issuing is global: one certificate policy
    direct_site, relay_site = relay.split("*.example.com:443 {")[1].split("*.example.com:8444 {")
    assert "@forged_local remote_ip private_ranges" in relay_site  # forged local addresses dropped
    assert "forged_local" not in direct_site  # home devices may use the direct listener
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


def test_relay_pki_server_cert_is_signed_by_the_ca_for_the_expected_name():
    pki = make_pki()
    ca = x509.load_pem_x509_certificate(pki["ca.crt"].encode())
    server = x509.load_pem_x509_certificate(pki["server.crt"].encode())
    server.verify_directly_issued_by(ca)
    names = server.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    assert names == [SERVER_NAME]
    assert make_pki()["ca.crt"] != pki["ca.crt"]  # a new key means a new CA


def test_relay_host_validation_blocks_config_injection():
    for good in ("203.0.113.25", "2001:db8::1", "relay.example.com", "vps1"):
        assert valid_relay_host(good), good
    for bad in ("", '1.2.3.4"\nauth.token = "x', "a b", "evil.com/path", "x" * 300, 'q"uote'):
        assert not valid_relay_host(bad), bad
