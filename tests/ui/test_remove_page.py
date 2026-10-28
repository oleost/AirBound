"""The "Turn off or remove" page in a real browser against a fake API."""

import json
import threading
from functools import partial
from http.server import ThreadingHTTPServer

import pytest

from test_users_step import STATIC, Panel, steps

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture()
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(Panel, directory=str(STATIC.parent)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    httpd.shutdown()


def test_remove_shows_the_plan_asks_first_then_what_is_left(server):
    fake = {"removed": False, "polls": 0, "started": False}
    tasks = [{"id": "stop", "label": "Stop remote access", "state": "done", "detail": ""},
             {"id": "forget", "label": "Forget AirBound's settings and keys", "state": "done", "detail": ""}]
    notes = ["Your Patreon or GitHub membership goes on until you cancel it there."]

    def handle(route):
        path = route.request.url.split("/api/", 1)[1]
        if path == "state":
            settings = {"mode": "hosted", "live": not fake["removed"], "removed": fake["removed"], "paused": False}
            body = {"settings": settings, "steps": steps(True), "complete": False, "landing": "off" if fake["removed"] else "overview"}
        elif path == "remove" and route.request.method == "POST":
            fake["started"] = True
            body = {"busy": True, "tasks": [{**tasks[0], "state": "running"}], "removed": False, "notes": []}
        elif path == "remove" and fake["started"]:
            fake["polls"] += 1
            fake["removed"] = fake["polls"] > 1
            body = {"busy": not fake["removed"], "tasks": tasks, "removed": fake["removed"], "notes": notes if fake["removed"] else []}
        elif path == "remove":
            body = {"busy": False, "tasks": [], "removed": False, "plan": [t["label"] for t in tasks], "notes": notes}
        else:
            body = {"ok": True}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.route("**/api/**", handle)
        page.goto(server + "#off")
        page.get_by_text("Forget AirBound's settings and keys").wait_for()
        assert page.get_by_text("Settings → Apps → AirBound → Uninstall", exact=False).count() == 1
        assert page.get_by_role("button", name="Turn off remote access").is_visible()  # live: pausing is offered
        page.get_by_role("button", name="Remove AirBound…").click()
        assert not fake["started"]  # nothing happens before the person confirms
        page.get_by_role("button", name="Remove AirBound", exact=True).click()
        page.get_by_role("heading", name="AirBound is removed").wait_for(timeout=10000)
        assert page.get_by_text(notes[0]).is_visible()
        assert page.get_by_role("button", name="Changed your mind? Set up AirBound again").is_visible()
        browser.close()


def test_every_page_links_to_turn_off_or_remove(server):
    def handle(route):
        body = {"settings": {"mode": "hosted", "live": False}, "steps": steps(True), "complete": False, "landing": "users"}
        if "/api/users" in route.request.url:
            body = {"users": [], "blocking": []}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.route("**/api/**", handle)
        page.goto(server + "#users")
        page.get_by_role("button", name="Turn off or remove AirBound").first.wait_for()
        browser.close()
