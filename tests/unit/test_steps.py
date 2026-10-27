from airbound.settings import Settings
from airbound.steps import compute, landing

DOMAIN = dict(domain="example.com", hostname="h", cloudflare_token="t", tls="cloudflare")


def by_id(steps):
    return {s.id: s for s in steps}


def test_fresh_install_opens_welcome_and_starts_with_the_connection_choice():
    steps = compute(Settings(), http_ok=False, blocking=["ola"], people=1)
    s = by_id(steps)
    assert landing(Settings(), steps) == "welcome"
    assert steps[0].id == "connection"  # the choice decides which steps follow
    assert "cloudflare" not in s and "hosted" not in s and "router" not in s
    assert not s["users"].locked  # can be done before choosing
    assert s["go_live"].locked
    assert s["go_live"].reason == "Needs: How to connect, Home Assistant settings, Two-step login"
    assert s["devices"].locked and s["devices"].reason == "Needs: Go live"


def test_relay_mode_shows_relay_step_not_router():
    steps = compute(Settings(mode="relay"), http_ok=True, blocking=[], people=1)
    ids = [s.id for s in steps]
    assert ids[:3] == ["connection", "cloudflare", "relay"]
    assert "router" not in ids and "hosted" not in ids
    assert by_id(steps)["go_live"].reason == "Needs: Your domain, Relay server"


def test_direct_mode_shows_router_step_not_relay():
    ids = [s.id for s in compute(Settings(mode="direct"), http_ok=True, blocking=[], people=1)]
    assert "router" in ids and "relay" not in ids and "hosted" not in ids


def test_hosted_mode_needs_no_domain_relay_or_router():
    steps = compute(Settings(mode="hosted"), http_ok=True, blocking=[], people=1)
    ids = [s.id for s in steps]
    assert ids == ["connection", "hosted", "ha_settings", "users", "go_live", "devices"]
    assert by_id(steps)["go_live"].reason == "Needs: Your AirBound address"
    linked = Settings(mode="hosted", tls="hosted", domain="airbound.dev", hostname="x7k2", hosted_key="k")
    s = by_id(compute(linked, http_ok=True, blocking=[], people=1))
    assert s["hosted"].done and s["hosted"].summary == "x7k2.airbound.dev"
    assert s["connection"].summary == "Through AirBound's hosted relay"
    assert not s["go_live"].locked


def test_private_relay_address_does_not_count_as_done():
    steps = compute(Settings(mode="relay", relay_host="172.30.33.1"), http_ok=True, blocking=[], people=1)
    assert not by_id(steps)["relay"].done


def test_lands_on_first_step_that_needs_attention_not_last_visited():
    settings = Settings(**DOMAIN, mode="direct", wizard_step="devices")
    steps = compute(settings, http_ok=True, blocking=["kari"], people=2)
    assert landing(settings, steps) == "users"


def test_fully_set_up_lands_on_overview_with_summaries():
    settings = Settings(**DOMAIN, mode="direct", live=True, external_port=35345, router_done=True, devices_done=True)
    steps = compute(settings, http_ok=True, blocking=[], people=2)
    assert landing(settings, steps) == "overview"
    s = by_id(steps)
    assert s["cloudflare"].summary == "h.example.com"
    assert s["go_live"].summary == "https://h.example.com:35345"
    assert s["router"].summary == "TCP port 35345 forwarded"
    assert s["users"].summary == "2 people, all set"
    assert all(step.done and not step.locked for step in steps)


def test_live_but_phone_step_unseen_still_opens_that_step():
    settings = Settings(**DOMAIN, mode="relay", relay_host="1.2.3.4", live=True)
    steps = compute(settings, http_ok=True, blocking=[], people=1)
    assert landing(settings, steps) == "devices"


def test_a_done_step_can_ask_for_attention():
    from airbound.settings import Settings
    from airbound.steps import compute

    s = Settings(mode="hosted", tls="hosted", domain="airbound.dev", hostname="x7k2", hosted_key="k", live=True)
    steps = {st.id: st for st in compute(s, http_ok=True, blocking=[], people=1, attention={"ha_settings": "Update recommended", "users": "x"})}
    assert steps["ha_settings"].done and steps["ha_settings"].attention == "Update recommended"
    # Only done steps carry it; a step that isn't done already shows what to do.
    steps = {st.id: st for st in compute(s, http_ok=False, blocking=[], people=1, attention={"ha_settings": "Update recommended"})}
    assert steps["ha_settings"].attention == ""
