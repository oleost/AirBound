import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "relay-server" / "controller"))

from airbound_relay import access  # noqa: E402
from airbound_relay.config import Config  # noqa: E402
from airbound_relay.db import Database  # noqa: E402
from airbound_relay.frp import Plugin  # noqa: E402
from airbound_relay.patreon import members_entitled, membership_in, valid_signature  # noqa: E402
from airbound_relay.web import RateLimit, claim_code  # noqa: E402

TIER = "29918172"
CONFIG = Config(domain="airbound.dev", campaign_id="17028040", tier_id=TIER, site_key="sitekey", free_users=frozenset({"owner"}))
INSTALL = "install-0123456789abcdef"


def member(status="active_patron", tiers=(TIER,), user="u1", campaign="17028040"):
    return {
        "type": "member",
        "attributes": {"patron_status": status},
        "relationships": {
            "currently_entitled_tiers": {"data": [{"id": t, "type": "tier"} for t in tiers]},
            "user": {"data": {"id": user, "type": "user"}},
            "campaign": {"data": {"id": campaign, "type": "campaign"}},
        },
    }


def linked(db, patreon_id="u1", entitled=True):
    db.set_entitled(patreon_id, entitled)
    return db.link_device(patreon_id, INSTALL, CONFIG.max_devices)


def proxy(device, key, domains, proxy_type="https"):
    return {"user": {"user": device, "metas": {"key": key}}, "proxy_type": proxy_type, "custom_domains": domains, "subdomain": ""}


def test_entitlement_needs_active_status_and_the_relay_tier():
    assert access.member_entitled(member(), TIER)
    assert not access.member_entitled(member(tiers=("free-tier",)), TIER)
    assert not access.member_entitled(member(status="declined_patron"), TIER)
    assert not access.member_entitled(member(status="former_patron"), TIER)


def test_identity_only_counts_this_campaign():
    identity = {"data": {"id": "u1"}, "included": [member(campaign="someone-else")]}
    assert membership_in(identity, "17028040", TIER) == ("u1", False)
    identity["included"].append(member())
    assert membership_in(identity, "17028040", TIER) == ("u1", True)


def test_member_list_maps_users():
    pages = [{"data": [member(user="a"), member(user="b", status="declined_patron")]}]
    assert members_entitled(pages, TIER) == {"a": True, "b": False}


def test_grace_period_after_lapse(tmp_path):
    db = Database(tmp_path / "db")
    db.set_entitled("u1", True, now=1000)
    account = db.set_entitled("u1", False, now=2000)
    assert account.lapsed_at == 2000
    assert access.state(account, "u1", frozenset(), 3, now=2000 + 2 * 86400) == access.GRACE
    assert access.state(account, "u1", frozenset(), 3, now=2000 + 4 * 86400) == access.EXPIRED
    assert db.set_entitled("u1", False, now=9000).lapsed_at == 2000  # the grace period doesn't restart
    assert db.set_entitled("u1", True, now=9500).lapsed_at is None


def test_never_paid_gets_no_grace(tmp_path):
    db = Database(tmp_path / "db")
    account = db.set_entitled("u2", False)
    assert access.state(account, "u2", frozenset(), 3) == access.EXPIRED
    assert access.state(None, "owner", frozenset({"owner"}), 3) == access.ACTIVE


def test_relinking_the_same_home_assistant_keeps_its_address(tmp_path):
    db = Database(tmp_path / "db")
    device, key = linked(db)
    again, new_key = db.link_device("u1", INSTALL, 2)
    assert again.hostname == device.hostname and new_key != key
    assert db.device_by_key(key) is None and db.device_by_key(new_key).id == device.id


def test_device_limit_unlinks_the_least_recent(tmp_path):
    db = Database(tmp_path / "db")
    db.set_entitled("u1", True)
    first, key1 = db.link_device("u1", "install-aaaaaaaaaaaaaaaa", 2, now=1)
    _second, key2 = db.link_device("u1", "install-bbbbbbbbbbbbbbbb", 2, now=2)
    db.seen(first.id, now=10)  # the first one is still in use
    _third, key3 = db.link_device("u1", "install-cccccccccccccccc", 2, now=11)
    assert db.device_by_key(key1) and db.device_by_key(key3)
    assert db.device_by_key(key2) is None


def test_plugin_login_needs_a_valid_key_for_that_device(tmp_path):
    db = Database(tmp_path / "db")
    device, key = linked(db)
    plugin = Plugin(CONFIG, db)
    assert plugin.handle("Login", {"user": device.id, "metas": {"key": key}})["reject"] is False
    assert plugin.handle("Login", {"user": device.id, "metas": {"key": "wrong"}})["reject"] is True
    assert plugin.handle("Login", {"user": "other", "metas": {"key": key}})["reject"] is True
    assert plugin.handle("Login", {"user": device.id})["reject"] is True


def test_plugin_proxy_only_for_own_hostname_with_server_side_limit(tmp_path):
    db = Database(tmp_path / "db")
    device, key = linked(db)
    plugin = Plugin(CONFIG, db)
    own = f"{device.hostname}.airbound.dev"
    result = plugin.handle("NewProxy", proxy(device.id, key, [own]))
    assert result["reject"] is False and result["unchange"] is False
    assert result["content"]["bandwidth_limit"] == "4MB" and result["content"]["bandwidth_limit_mode"] == "server"
    for domains in (["airbound.dev"], ["relay.airbound.dev"], ["x.airbound.dev"], [own, "x.airbound.dev"]):
        assert plugin.handle("NewProxy", proxy(device.id, key, domains))["reject"] is True, domains
    assert plugin.handle("NewProxy", proxy(device.id, key, [own], proxy_type="tcp"))["reject"] is True


def test_site_key_may_only_publish_the_domain_itself(tmp_path):
    plugin = Plugin(CONFIG, Database(tmp_path / "db"))
    assert plugin.handle("NewProxy", proxy("site", "sitekey", ["airbound.dev"]))["reject"] is False
    assert plugin.handle("NewProxy", proxy("site", "sitekey", ["x.airbound.dev"]))["reject"] is True
    assert plugin.handle("Login", {"user": "intruder", "metas": {"key": "sitekey"}})["reject"] is True


def test_ping_disconnects_after_membership_ends(tmp_path):
    db = Database(tmp_path / "db")
    device, key = linked(db)
    plugin = Plugin(CONFIG, db)
    ping = {"user": {"user": device.id, "metas": {"key": key}}}
    assert plugin.handle("Ping", ping, now=1000)["reject"] is False
    db.set_entitled("u1", False, now=1000)
    assert plugin.handle("Ping", ping, now=1000 + 86400)["reject"] is False  # grace period
    assert plugin.handle("Ping", ping, now=1000 + 4 * 86400)["reject"] is True


def test_claim_key_is_handed_out_once(tmp_path):
    db = Database(tmp_path / "db")
    claim_id, poll = db.new_claim(INSTALL)
    assert db.claim_for_poll(claim_id, "wrong") is None
    device, key = linked(db)
    db.set_claim(claim_id, "approved", patreon_id="u1", device_id=device.id, device_key=key)
    assert db.claim_for_poll(claim_id, poll)["device_key"] == key
    db.take_claim_key(claim_id)
    assert db.claim_for_poll(claim_id, poll)["device_key"] is None


def test_claims_expire(tmp_path):
    db = Database(tmp_path / "db")
    claim_id, poll = db.new_claim(INSTALL, now=1000)
    assert db.claim_for_poll(claim_id, poll, now=1000 + 60)
    assert db.claim_for_poll(claim_id, poll, now=1000 + 901) is None


def test_claim_code_is_stable_and_readable():
    code = claim_code("abc")
    assert code == claim_code("abc") and len(code) == 7 and code[3] == "-"
    assert not set(code) & set("01IO")


def test_webhook_signature():
    import hashlib
    import hmac

    body = b'{"data":{}}'
    good = hmac.new(b"s3cret", body, hashlib.md5).hexdigest()
    assert valid_signature("s3cret", body, good)
    assert not valid_signature("s3cret", body, "0" * 32)
    assert not valid_signature("s3cret", body, "")


def test_rate_limit():
    limit = RateLimit(2, 60)
    assert limit.allow("a", now=0) and limit.allow("a", now=1)
    assert not limit.allow("a", now=2)
    assert limit.allow("b", now=2)
    assert limit.allow("a", now=61.5)


def test_terms_say_hobby_project_without_guarantee_and_are_linked_everywhere():
    from airbound_relay import pages

    terms = pages.terms(CONFIG)
    for phrase in ("hobby project", "as is", "not liable", "not buying a service", "30 days"):
        assert phrase in terms
    assert 'href="/terms"' in pages.page("x", "", CONFIG)
    assert 'href="/terms"' in pages.confirm_code("c", "ABC123")
    assert 'href="/terms"' in pages.needs_membership(CONFIG, "c")


def test_github_sponsors_is_only_mentioned_once_it_is_set_up():
    from dataclasses import replace

    from airbound_relay import pages

    off = CONFIG
    on = replace(CONFIG, github_client_id="id", github_client_secret="secret", github_sponsorable="oleost")
    for render in (pages.home, pages.privacy, pages.terms):
        assert "Sponsor" not in render(off) and "sponsorship" not in render(off), render.__name__
        assert "GitHub" in render(on), render.__name__
    assert "GitHub Sponsors" in pages.home(on) and "GitHub Sponsors" in pages.terms(on)


def test_fair_use_slows_a_device_down_past_its_monthly_allowance(tmp_path):
    from airbound_relay.limits import GB, Limits

    db = Database(tmp_path / "db.sqlite")
    device, key = linked(db)
    limits = Limits(CONFIG, db)
    plugin = Plugin(CONFIG, db, limits)
    now = 1_790_000_000.0
    name = f"{device.id}.airbound"
    fqdn = f"{device.hostname}.airbound.dev"

    assert plugin.handle("NewProxy", proxy(device.id, key, [fqdn]), now)["content"]["bandwidth_limit"] == CONFIG.bandwidth
    assert limits.record({name: 0}, None, now) == []  # baseline
    assert limits.record({name: 100 * GB}, None, now) == []
    ping = {"user": {"user": device.id, "metas": {"key": key}}}
    assert plugin.handle("Ping", ping, now)["reject"] is False

    alerts = limits.record({name: 50 * GB}, None, now)  # the counter went down: a new day, all of it is new
    assert db.traffic("2026-09", device.id) == 150 * GB and limits.device_over(device.id, now) is False
    alerts = limits.record({name: 250 * GB}, None, now)
    assert len(alerts) == 1 and "passed 300 GB" in alerts[0]
    assert plugin.handle("Ping", ping, now)["reject"] is True  # reconnect for the new speed
    result = plugin.handle("NewProxy", proxy(device.id, key, [fqdn]), now)
    assert result["content"]["bandwidth_limit"] == CONFIG.throttled_bandwidth
    assert plugin.handle("Ping", ping, now)["reject"] is False
    assert limits.record({name: 260 * GB}, None, now) == []  # alerted once


def test_fair_use_server_allowance_slows_everyone_and_warns_first(tmp_path):
    from airbound_relay.limits import GB, Limits

    db = Database(tmp_path / "db.sqlite")
    device, _key = linked(db)
    limits = Limits(CONFIG, db)
    now = 1_790_000_000.0
    limits.record({}, 0, now)
    assert "of its allowance" in limits.record({}, 12_500 * GB, now)[0]
    assert limits.bandwidth_for(device.id, now) == CONFIG.bandwidth
    assert "past its allowance" in limits.record({}, 15_100 * GB, now)[0]
    assert limits.bandwidth_for(device.id, now) == CONFIG.emergency_bandwidth
    later = now + 40 * 86400  # next month
    assert limits.bandwidth_for(device.id, later) == CONFIG.bandwidth


def test_fair_use_keeps_three_months():
    from airbound_relay.limits import month_of, months_ago

    now = 1_790_000_000.0  # 2026-09
    assert month_of(now) == "2026-09"
    assert months_ago(now, 2) == "2026-07"


def test_github_monthly_sponsorship_at_the_minimum_gives_access():
    from airbound_relay.github import sponsorship_counts

    monthly = lambda cents: {"isOneTimePayment": False, "tier": {"monthlyPriceInCents": cents, "isOneTime": False}}
    assert sponsorship_counts(monthly(100), 100)
    assert sponsorship_counts(monthly(500), 100)
    assert not sponsorship_counts(monthly(50), 100)
    assert not sponsorship_counts({"isOneTimePayment": True, "tier": {"monthlyPriceInCents": 1000, "isOneTime": True}}, 100)
    assert not sponsorship_counts(None, 100)


def test_github_webhook_signature():
    import hashlib, hmac
    from airbound_relay.github import valid_signature

    body = b'{"action":"created"}'
    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert valid_signature("s3cret", body, good)
    assert not valid_signature("s3cret", body, "sha256=" + "0" * 64)
    assert not valid_signature("s3cret", body, "")


async def test_github_sync_sets_access_for_github_accounts_only(tmp_path, monkeypatch):
    from airbound_relay.github import GitHub

    db = Database(tmp_path / "db.sqlite")
    db.set_entitled("u1", True)  # a Patreon member: untouched by the GitHub sync
    db.set_entitled("gh:7", True)  # was sponsoring, stopped
    config = Config(domain="airbound.dev", github_client_id="c", github_client_secret="s", github_token="t", github_sponsorable="oleost")
    github = GitHub(config, db, None)

    async def fake_graphql(_token, _query, _variables):
        node = lambda uid, cents: {"isOneTimePayment": False, "tier": {"monthlyPriceInCents": cents, "isOneTime": False}, "sponsorEntity": {"databaseId": uid}}
        return {"user": {"sponsorshipsAsMaintainer": {"pageInfo": {"hasNextPage": False}, "nodes": [node(42, 100), node(43, 50)]}}}

    monkeypatch.setattr(github, "_graphql", fake_graphql)
    assert await github.sync_sponsors() == 1
    assert db.account("gh:42").entitled and not db.account("gh:43").entitled
    assert not db.account("gh:7").entitled and db.account("gh:7").lapsed_at  # grace period starts
    assert db.account("u1").entitled


def test_join_and_manage_links_follow_the_account():
    config = Config(domain="airbound.dev", tier_id=TIER, github_sponsorable="oleost")
    assert config.join_url_for("gh:42") == "https://github.com/sponsors/oleost"
    assert "patreon.com" in config.join_url_for("12345")


def test_ops_report_has_the_health_fields(tmp_path, monkeypatch):
    from airbound_relay import ops

    db = Database(tmp_path / "db.sqlite")
    linked(db)
    monkeypatch.setattr(ops, "health_problems", lambda: [])  # reads Linux-only load and memory
    report = ops.report(CONFIG, db, 60.0)
    for key in ("reboot_required", "days_since_unattended_upgrade", "disk_used_percent", "frp_build", "website_certificate_days_left", "devices"):
        assert key in report
    assert report["devices"] == 1 and report["accounts_with_access"] == 1


async def test_a_home_assistant_can_unlink_itself_with_its_key(tmp_path):
    from aiohttp.test_utils import make_mocked_request

    from airbound_relay.web import Web

    db = Database(tmp_path / "db")
    device, key = linked(db)
    web_app = Web(CONFIG, db, None, lambda: None)

    def unlink(token):
        return web_app.device_unlink(make_mocked_request("DELETE", "/api/v1/device", headers={"Authorization": f"Bearer {token}"}))

    assert (await unlink("made-up")).status == 404
    assert db.device_by_key(key).id == device.id
    assert (await unlink(key)).status == 200
    assert db.device_by_key(key) is None and db.devices_of("u1") == []
    assert (await unlink(key)).status == 404  # already gone
    again, _ = db.link_device("u1", INSTALL, CONFIG.max_devices)
    assert again.hostname != device.hostname  # a fresh start gets a new address


def test_a_key_belongs_to_the_first_machine_that_uses_it(tmp_path):
    db = Database(tmp_path / "db")
    device, key = linked(db)
    plugin = Plugin(CONFIG, db)

    def login(machine):
        return plugin.handle("Login", {"user": device.id, "metas": {"key": key, "machine": machine}}, now=500)

    assert login("home")["reject"] is False
    assert login("home")["reject"] is False
    refused = login("copy")  # the same key from a restored backup elsewhere
    assert refused["reject"] is True and "other machine" in refused["reject_reason"]
    assert db.device_by_key(key).foreign_seen == 500
    assert plugin.handle("Login", {"user": device.id, "metas": {"key": key}})["reject"] is True  # no machine at all
    ping = {"user": {"user": device.id, "metas": {"key": key, "machine": "copy"}}}
    assert plugin.handle("Ping", ping)["reject"] is True


def test_relinking_lets_the_new_machine_claim_the_key(tmp_path):
    db = Database(tmp_path / "db")
    device, key = linked(db)
    plugin = Plugin(CONFIG, db)
    assert plugin.handle("Login", {"user": device.id, "metas": {"key": key, "machine": "old"}})["reject"] is False
    again, new_key = db.link_device("u1", INSTALL, CONFIG.max_devices)  # a Patreon login on the new machine
    assert again.hostname == device.hostname
    assert plugin.handle("Login", {"user": device.id, "metas": {"key": key, "machine": "old"}})["reject"] is True  # old key gone
    assert plugin.handle("Login", {"user": device.id, "metas": {"key": new_key, "machine": "new"}})["reject"] is False
    assert db.device_by_key(new_key).foreign_seen is None


def test_an_add_on_without_machines_works_until_one_claims_the_key(tmp_path):
    db = Database(tmp_path / "db")
    device, key = linked(db)
    plugin = Plugin(CONFIG, db)
    assert plugin.handle("Login", {"user": device.id, "metas": {"key": key}})["reject"] is False
    assert plugin.handle("Login", {"user": device.id, "metas": {"key": key, "machine": "home"}})["reject"] is False
    assert plugin.handle("Login", {"user": device.id, "metas": {"key": key}})["reject"] is True


def test_older_databases_get_the_machine_columns(tmp_path):
    import sqlite3

    path = tmp_path / "db"
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE devices (id TEXT PRIMARY KEY, patreon_id TEXT NOT NULL, install_id TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE,"
        " hostname TEXT NOT NULL UNIQUE, created REAL NOT NULL, last_seen REAL, revoked INTEGER NOT NULL DEFAULT 0);"
        "INSERT INTO devices VALUES ('d1', 'u1', 'i', 'h', 'otter-comet-maple', 1, NULL, 0);"
    )
    old.close()
    device = Database(path).device("d1")
    assert device.hostname == "otter-comet-maple" and device.machine_hash is None and device.foreign_seen is None


async def test_status_and_unlink_only_from_the_machine_that_uses_the_key(tmp_path):
    from aiohttp.test_utils import make_mocked_request

    from airbound_relay.web import Web

    db = Database(tmp_path / "db")
    device, key = linked(db)
    web_app = Web(CONFIG, db, None, lambda: None)

    def request(method, machine):
        headers = {"Authorization": f"Bearer {key}", "X-AirBound-Machine": machine}
        return make_mocked_request(method, "/api/v1/device", headers=headers)

    assert (await web_app.device_status(request("GET", "home"))).status == 200
    assert (await web_app.device_status(request("GET", "copy"))).status == 409
    assert (await web_app.device_unlink(request("DELETE", "copy"))).status == 409
    assert db.device_by_key(key).id == device.id  # a copied backup can't take the address away
    status = await web_app.device_status(request("GET", "home"))
    import json

    assert json.loads(status.body)["foreign_seen"]  # the owner's AirBound can warn about the copy
    assert (await web_app.device_unlink(request("DELETE", "home"))).status == 200


async def test_the_website_has_a_tab_icon(tmp_path):
    from aiohttp.test_utils import make_mocked_request

    from airbound_relay import pages
    from airbound_relay.web import Web

    web_app = Web(CONFIG, Database(tmp_path / "db"), None, lambda: None)
    home = await web_app.home(make_mocked_request("GET", "/"))
    assert '<link rel="icon" href="/favicon.svg"' in home.text
    icon = await web_app.icon(make_mocked_request("GET", "/favicon.svg"))
    assert icon.content_type == "image/svg+xml" and icon.text == pages.ICON
    assert {"/favicon.svg", "/favicon.ico"} <= {r.resource.canonical for r in web_app.app().router.routes()}
