import json

from airbound.mqtt import AVAILABILITY, ENTITIES, TRIGGERS, discovery_messages, entity_id
from airbound.reporting import last_connection


def test_every_entity_has_unique_id_device_and_short_entity_id():
    messages = dict(discovery_messages())
    for key, (component, _, _) in ENTITIES.items():
        payload = messages[f"homeassistant/{component}/airbound/{key}/config"]
        assert payload["unique_id"] == f"airbound_{key}"
        assert payload["default_entity_id"] == entity_id(key) == f"{component}.airbound_{key}"
        assert payload["availability_topic"] == AVAILABILITY
        assert payload["device"]["name"] == "AirBound remote access"
        assert "configuration_url" not in payload["device"]  # only with a known slug
        json.dumps(payload)  # must serialise


def test_device_links_to_the_panel_when_slug_is_known():
    payload = dict(discovery_messages("cfebc6a4_airbound"))["homeassistant/sensor/airbound/status/config"]
    assert payload["device"]["configuration_url"] == "homeassistant://hassio/ingress/cfebc6a4_airbound"


def test_remote_access_is_a_connectivity_binary_sensor():
    payload = dict(discovery_messages())["homeassistant/binary_sensor/airbound/remote_access/config"]
    assert payload["device_class"] == "connectivity"
    assert (payload["payload_on"], payload["payload_off"]) == ("on", "off")


def test_device_triggers_for_the_automation_editor():
    messages = dict(discovery_messages())
    for key, (trigger_type, subtype) in TRIGGERS.items():
        payload = messages[f"homeassistant/device_automation/airbound/{key}/config"]
        assert payload["automation_type"] == "trigger"
        assert (payload["type"], payload["subtype"]) == (trigger_type, subtype)
        assert payload["topic"] == f"airbound/trigger/{key}"


def access(host, ip, ts):
    return json.dumps({"ts": ts, "request": {"host": host, "client_ip": ip, "uri": "/"}})


def test_last_connection_picks_newest_request_for_the_hostname():
    lines = [
        access("h.example.com:35345", "198.51.100.1", 1000.0),
        access("other.example.com", "203.0.113.9", 2000.0),
        "not json",
        access("h.example.com", "198.51.100.2", 1500.0),
    ]
    found = last_connection(lines, "h.example.com")
    assert found["ip"] == "198.51.100.2"
    assert found["at"].startswith("1970-01-01T00:25:00")


def test_last_connection_none_when_no_matching_requests():
    assert last_connection([access("other.example.com", "1.2.3.4", 1.0)], "h.example.com") is None
