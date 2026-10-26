from airbound.settings import Settings
from airbound.steps import compute, landing

DOMAIN = dict(domain="example.com", hostname="h", cloudflare_token="t", tls="cloudflare")


def by_id(steps):
    return {s.id: s for s in steps}


def test_fresh_install_opens_welcome_and_locks_go_live():
    steps = compute(Settings(), http_ok=False, blocking=["ola"], people=1)
    s = by_id(steps)
    assert landing(Settings(), steps) == "welcome"
    assert not s["cloudflare"].locked and not s["users"].locked  # can be done in any order
    assert s["go_live"].locked
    assert s["go_live"].reason == "Needs: Your domain, How to connect, Home Assistant settings, Two-step login"
    assert s["devices"].locked and s["devices"].reason == "Needs: Go live"


def test_relay_mode_shows_relay_step_not_router():
    steps = compute(Settings(mode="relay"), http_ok=True, blocking=[], people=1)
    ids = [s.id for s in steps]
    assert "relay" in ids and "router" not in ids
    assert by_id(steps)["go_live"].reason == "Needs: Your domain, Relay server"


def test_direct_mode_shows_router_step_not_relay():
    ids = [s.id for s in compute(Settings(mode="direct"), http_ok=True, blocking=[], people=1)]
    assert "router" in ids and "relay" not in ids


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
