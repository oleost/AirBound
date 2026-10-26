from airbound.caddy import render_caddyfile
from airbound.settings import Settings


def test_wildcard_site_hides_hostname():
    caddyfile = render_caddyfile(
        Settings(domain="example.com", hostname="k7f3q9", cloudflare_token="secret-token"), "homeassistant:80"
    )
    assert "*.example.com:443 {" in caddyfile
    assert "@airbound host k7f3q9.example.com" in caddyfile
    assert "dns cloudflare {$CF_API_TOKEN}" in caddyfile
    assert "abort" in caddyfile
    assert "reverse_proxy homeassistant:80" in caddyfile
    assert "secret-token" not in caddyfile


def test_login_flow_goes_through_guard():
    caddyfile = render_caddyfile(Settings(domain="example.com", hostname="h", tls="internal"), "homeassistant:80")
    assert "@login path /auth/login_flow /auth/login_flow/*" in caddyfile
    assert "reverse_proxy @login 127.0.0.1:8100" in caddyfile
    assert "local_certs" in caddyfile


def test_settings_configured():
    assert not Settings().is_configured
    assert not Settings(domain="example.com", hostname="h").is_configured
    assert Settings(domain="example.com", hostname="h", tls="internal").is_configured
    assert Settings(domain="example.com", hostname="h", cloudflare_token="t").is_configured
