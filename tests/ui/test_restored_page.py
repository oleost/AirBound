"""The page after a restore from a backup, in a real browser against a fake API."""

import json

import pytest

from test_users_step import server, steps  # noqa: F401  (server is a fixture)

playwright = pytest.importorskip("playwright.sync_api")


def test_restored_hosted_setup_waits_for_confirmation_then_asks_for_patreon(server):  # noqa: F811
    fake = {"restored": True, "confirmed": False, "went_live": False}

    def handle(route):
        path = route.request.url.split("/api/", 1)[1]
        settings = {"mode": "hosted", "tls": "hosted", "fqdn": "otter-comet-maple.airbound.dev", "restored": fake["restored"],
                    "live": fake["restored"], "has_hosted_key": fake["restored"], "external_port": 443}
        if path == "restored":
            fake["confirmed"], fake["restored"] = True, False
            settings.update(restored=False, live=False, has_hosted_key=False)
        if path == "go-live" and route.request.method == "POST":
            fake["went_live"] = True
        if path in ("state", "restored"):
            body = {"settings": settings, "steps": steps(True), "complete": False, "landing": "restored" if fake["restored"] else "hosted"}
        elif path == "hosted":
            body = {"linked": False, "fqdn": "", "state": "idle"}
        else:
            body = {"ok": True}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.route("**/api/**", handle)
        page.goto(server + "#overview")  # an old link must not skip the question
        page.get_by_role("heading", name="AirBound was restored from a backup").wait_for()
        assert page.get_by_text("airbound.dev asks for your Patreon or GitHub login again", exact=False).is_visible()
        assert page.get_by_text("Turn the old Home Assistant off first", exact=False).is_visible()
        assert not fake["confirmed"] and not fake["went_live"]
        page.get_by_role("button", name="Continue on this Home Assistant").click()
        page.get_by_text("this Home Assistant keeps", exact=False).wait_for(timeout=5000)
        assert page.get_by_text("otter-comet-maple.airbound.dev", exact=False).first.is_visible()
        assert fake["confirmed"] and not fake["went_live"]
        browser.close()
