from airbound import ha_settings
from airbound.mqtt import AVAILABILITY, discovery_messages, removal_topics
from airbound.remove import plan
from airbound.settings import Settings, Store
from airbound.steps import landing

WANTED = ha_settings.WANTED
BEFORE = {"use_x_forwarded_for": None, "trusted_proxies": None, "ip_ban_enabled": None, "login_attempts_threshold": None}


def person(id_, local_only=False):
    return {"id": id_, "name": id_.title(), "mfa": False, "local_only": local_only}


def ids(result):
    return [t.id for t in result.tasks]


def test_removing_a_hosted_setup_undoes_everything_airbound_did():
    s = Settings(mode="hosted", tls="hosted", hostname="otter-comet-maple", domain="airbound.dev", hosted_key="k", live=True,
                 auto_limited=("kid",), ha_http_before=BEFORE)
    result = plan(s, dict(WANTED), [person("kid", local_only=True), person("me")])
    assert ids(result) == ["stop", "users", "hosted", "sensors", "ha_settings", "forget"]
    assert any("membership" in n for n in result.notes)
    assert not any("router" in n or "Cloudflare" in n for n in result.notes)


def test_removing_direct_mode_deletes_dns_and_lists_the_router_and_token():
    s = Settings(mode="direct", tls="cloudflare", hostname="h", domain="example.com", zone_id="z", cloudflare_token="t",
                 live=True, router_done=True, external_port=9443, ha_http_before=BEFORE)
    result = plan(s, dict(WANTED), [])
    assert "dns" in ids(result) and "users" not in ids(result) and "hosted" not in ids(result)
    assert any("TCP port 9443" in n for n in result.notes)
    assert any("API token" in n for n in result.notes)


def test_an_automatic_port_mapping_is_closed_not_left_to_the_person():
    s = Settings(mode="direct", port_mapping="upnp", router_done=True, external_port=9443)
    result = plan(s, {}, [])
    assert "router" in ids(result) and result.tasks[ids(result).index("router")].label == "Close port 9443 on your router"
    assert not any("port forward" in n for n in result.notes)


async def test_a_router_that_doesnt_answer_leaves_the_lease_to_run_out(tmp_path, monkeypatch):
    from airbound import portmap
    from airbound.remove import Removal, Task

    async def gateway(_supervisor):
        return "192.168.1.1"

    async def refuse(*_args):
        raise portmap.PortMapError("no answer")

    monkeypatch.setattr(portmap, "gateway_of", gateway)
    monkeypatch.setattr(portmap, "close_port", refuse)
    store = Store(tmp_path / "settings.json")
    store.update(mode="direct", port_mapping="natpmp", external_port=443)
    note = await Removal(store, None, None, None, None, None)._router(Task("router", ""))
    assert "within an hour" in note


def test_own_relay_server_is_left_for_the_person():
    s = Settings(mode="relay", relay_host="203.0.113.25", tls="cloudflare", cloudflare_token="t", zone_id="z", hostname="h", domain="example.com")
    result = plan(s, {}, [])
    assert "dns" in ids(result)  # the name points at the relay; it goes
    relay = next(n for n in result.notes if "203.0.113.25" in n)
    assert "hosting provider" in relay and "keys AirBound forgets" in relay and "firewall" in relay


def test_partly_set_up_removes_only_what_was_done():
    result = plan(Settings(mode="hosted"), {}, [])
    assert ids(result) == ["stop", "sensors", "forget"]


def test_ha_settings_unknown_from_before_are_listed_not_changed():
    result = plan(Settings(), dict(WANTED), [])
    assert "ha_settings" not in ids(result)
    assert any("reverse proxy" in n for n in result.notes)


def test_the_first_values_are_kept_through_later_applies():
    original = {"use_x_forwarded_for": True, "trusted_proxies": ["172.30.33.0/24"], "server_port": 8123}
    recorded = ha_settings.before(original, {})
    assert recorded["use_x_forwarded_for"] is True and recorded["ip_ban_enabled"] is None
    assert ha_settings.before(ha_settings.merged(original), recorded) == recorded  # applying again changes nothing


def test_restoring_puts_back_only_what_still_has_airbounds_value():
    original = {"use_x_forwarded_for": True, "trusted_proxies": ["172.30.33.0/24"], "server_port": 8123}
    recorded = ha_settings.before(original, {})
    now = {**ha_settings.merged(original), "login_attempts_threshold": 20}  # the person changed this one since
    back = ha_settings.restored(now, recorded)
    assert back["server_port"] == 8123
    assert back["trusted_proxies"] == ["172.30.33.0/24"]  # a reverse proxy needed it before AirBound
    assert "ip_ban_enabled" not in back  # wasn't set before
    assert back["login_attempts_threshold"] == 20
    assert ha_settings.restored(back, recorded) is None  # nothing left to do


def test_mqtt_removal_clears_every_entity_trigger_and_state():
    topics = removal_topics()
    assert {t for t, _ in discovery_messages()} <= set(topics)
    assert AVAILABILITY in topics and "airbound/status/state" in topics


def test_a_removed_airbound_opens_on_the_removal_page(tmp_path):
    store = Store(tmp_path / "settings.json")
    store.update(mode="hosted", hosted_key="k", install_id="i", live=True)
    store.reset(removed=True, install_id="i")
    reloaded = Store(tmp_path / "settings.json").settings
    assert reloaded.removed and reloaded.install_id == "i"
    assert not (reloaded.mode or reloaded.hosted_key or reloaded.live)
    assert landing(store.settings, []) == "off"
