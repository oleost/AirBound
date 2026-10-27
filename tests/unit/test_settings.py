import json
import re

from airbound.ha_settings import changes, merged
from airbound.settings import Store, random_hostname, valid_hostname


def test_valid_hostname():
    assert valid_hostname("kitchen-42")
    assert valid_hostname("a")
    for bad in ("", "-start", "end-", "has space", "dots.not.allowed", "UPPER", "æøå", "a" * 64):
        assert not valid_hostname(bad), bad


def test_store_persists_and_hides_token(tmp_path):
    path = tmp_path / "settings.json"
    store = Store(path)
    store.update(domain="example.com", hostname="abc", cloudflare_token="secret")
    assert Store(path).settings.fqdn == "abc.example.com"
    public = Store(path).settings.public()
    assert "cloudflare_token" not in public
    assert public["has_token"] is True
    assert json.loads(path.read_text())["cloudflare_token"] == "secret"


def test_url_includes_port_only_when_not_443():
    from airbound.settings import Settings

    assert Settings(domain="example.com", hostname="h").url == "https://h.example.com"
    assert Settings(domain="example.com", hostname="h", external_port=8443).url == "https://h.example.com:8443"
    assert Settings().url == ""


def test_store_ignores_unknown_keys(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"domain": "example.com", "from_a_newer_version": 1}))
    assert Store(path).settings.domain == "example.com"


def test_random_hostname_is_dns_safe_and_unique():
    names = {random_hostname() for _ in range(50)}
    assert len(names) == 50
    assert all(re.fullmatch(r"[a-z][a-z0-9]{11}", n) for n in names)


def test_ha_changes_in_plain_words():
    default = {"server_port": 80, "ip_ban_enabled": True, "login_attempts_threshold": -1}
    listed = {c["label"]: (c["current"], c["new"]) for c in changes(default)}
    assert listed == {
        "Trust the address AirBound passes on": ("Not set", "Yes"),
        "Trusted proxy": ("Not set", "172.30.33.0/24"),
        "Failed logins before blocking": ("Never", "50"),
    }


def test_merged_keeps_other_settings_and_drops_metadata():
    config = merged({"server_port": 80, "created_at": "x", "error": None, "use_x_forwarded_for": False})
    assert config["server_port"] == 80
    assert config["use_x_forwarded_for"] is True
    assert "created_at" not in config and "error" not in config
