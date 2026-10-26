from airbound.checks import check_http, check_mfa, check_trusted_networks

GOOD_HTTP = {
    "use_x_forwarded_for": True,
    "trusted_proxies": ["172.30.33.0/24"],
    "ip_ban_enabled": True,
    "login_attempts_threshold": 10,
}


def person(name, mfa=False, local_only=False):
    return {"id": name, "name": name, "mfa": mfa, "local_only": local_only, "owner": False, "you": False}


def test_mfa_check_names_who_blocks_and_links_to_fix():
    result = check_mfa([person("ola"), person("kari", mfa=True), person("bestemor", local_only=True)])
    assert not result.ok
    assert result.detail.startswith("ola can't log in from outside")
    assert result.fix_step == "users"


def test_mfa_check_ok():
    assert check_mfa([person("kari", mfa=True)]).ok


def test_http_ok():
    assert check_http(GOOD_HTTP).ok


def test_http_default_config_fails_with_explanation():
    result = check_http({"ip_ban_enabled": True, "login_attempts_threshold": -1})
    assert not result.ok
    assert "doesn't trust AirBound" in result.detail
    assert "never blocked" in result.detail
    assert result.fix_step == "ha_settings"


def test_http_too_broad_proxy_fails():
    result = check_http({**GOOD_HTTP, "trusted_proxies": ["172.16.0.0/12"]})
    assert not result.ok
    assert "broader" in result.detail


def test_trusted_networks():
    assert check_trusted_networks("default_config:\n").ok
    assert not check_trusted_networks("homeassistant:\n  auth_providers:\n    - type: trusted_networks\n").ok
