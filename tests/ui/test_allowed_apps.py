"""The Allowed apps card in a real browser against a fake API: no Home Assistant needed."""

import json

import pytest
from test_users_step import playwright, server, steps  # noqa: F401

ALEXA = "https://pitangui.amazon.com/"
EVIL = "https://evil.example/<img src=x onerror=alert(1)>"


def fake_api(fake):
    def handle(route):
        path = route.request.url.split("/api/", 1)[1]
        posted = json.loads(route.request.post_data or "{}")
        if path == "state":
            body = {
                "settings": {"mode": "hosted", "live": True, "url": "https://x.airbound.dev", "require_mfa": True},
                "steps": steps(True),
                "complete": True,
                "landing": "overview",
            }
        elif path == "status":
            body = {"checks": [], "refused_logins": 0}
        elif path == "bans":
            body = {"bans": []}
        elif path == "settings":
            fake["enabled"] = posted["restrict_clients"]
            body = {"settings": {}}
        else:
            if path == "clients/allow":
                if not posted["client"].startswith("https://"):
                    route.fulfill(status=400, content_type="application/json", body=json.dumps({"error": "Enter the app's web address"}))
                    return
                fake["allowed"].append({"client": posted["client"], "name": ""})
                fake["blocked"] = [b for b in fake["blocked"] if b["client"] != posted["client"]]
            if path == "clients/remove":
                fake["allowed"] = [a for a in fake["allowed"] if a["client"] != posted["client"]]
            body = {
                "enabled": fake["enabled"],
                "official": [{"client": "https://home-assistant.io/iOS", "name": "Home Assistant app for iPhone, iPad and Mac"}],
                "allowed": fake["allowed"],
                "blocked": fake["blocked"],
            }
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    return handle


@pytest.fixture()
def page(server):  # noqa: F811
    fake = {"enabled": True, "allowed": [], "blocked": [{"client": ALEXA, "name": "Alexa (US and Brazil)", "count": 2, "last": "2026-11-01T18:00:00Z"}]}
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        tab = browser.new_page()
        tab.route("**/api/**", fake_api(fake))
        tab.goto(server + "#overview")
        tab.get_by_role("heading", name="Allowed apps").wait_for()
        tab.fake = fake
        yield tab
        browser.close()


def test_stopped_app_is_allowed_only_after_a_warning(page):
    assert "Alexa (US and Brazil)" in page.inner_text("#allowed-apps")
    page.get_by_role("button", name="Allow…").click()
    page.get_by_text("Did you just try to connect this app yourself?").wait_for()
    assert page.fake["allowed"] == []
    page.get_by_role("button", name="Yes, I started this: allow it").click()
    page.get_by_role("button", name="Remove", exact=True).wait_for()
    assert page.fake["allowed"][0]["client"] == ALEXA
    assert page.get_by_role("heading", name="Recently stopped").count() == 0


def test_app_is_added_by_address_and_removed(page):
    page.fill("#client-address", "https://example.com/")
    page.get_by_role("button", name="Allow", exact=True).click()
    page.get_by_role("button", name="Remove", exact=True).wait_for()
    assert page.input_value("#client-address") == ""
    page.get_by_role("button", name="Remove", exact=True).click()
    page.get_by_role("button", name="Remove", exact=True).wait_for(state="detached")
    assert page.fake["allowed"] == []


def test_wrong_address_shows_why(page):
    page.fill("#client-address", "not an address")
    page.get_by_role("button", name="Allow", exact=True).click()
    page.get_by_text("Enter the app's web address").wait_for()
    assert page.input_value("#client-address") == "not an address"


def test_turning_off_asks_first_and_can_be_undone(page):
    switch = "Only let allowed apps log in from outside"
    page.get_by_role("checkbox", name=switch).evaluate("el => el.click()")
    page.get_by_text("Turn this off?").wait_for()
    assert page.fake["enabled"] is True
    page.get_by_role("button", name="Turn it off").first.click()
    page.get_by_text("Any app or website can ask you to log in").wait_for()
    assert page.fake["enabled"] is False
    page.get_by_role("checkbox", name=switch).evaluate("el => el.click()")
    page.get_by_role("heading", name="Allowed", exact=True).wait_for()
    assert page.fake["enabled"] is True


def test_a_name_from_outside_is_shown_as_text(page):
    page.fake["blocked"].append({"client": EVIL, "name": "", "count": 1, "last": "2026-11-01T18:00:00Z"})
    page.reload()
    page.get_by_role("heading", name="Allowed apps").wait_for()
    page.get_by_text(EVIL).wait_for()
    assert page.locator("#allowed-apps img").count() == 0
