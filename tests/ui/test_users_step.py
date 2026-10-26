"""The panel in a real browser against a fake API: no Home Assistant needed."""

import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

STATIC = Path(__file__).resolve().parents[2] / "airbound" / "airbound" / "static"


def steps(users_done: bool) -> list[dict]:
    def step(id_, title, done=False, locked=False, reason=""):
        return {"id": id_, "title": title, "done": done, "locked": locked, "reason": reason, "summary": ""}

    return [
        step("connection", "How to connect", done=True),
        step("hosted", "Your AirBound address", done=True),
        step("ha_settings", "Home Assistant settings", done=True),
        step("users", "Two-step login", done=users_done),
        step("go_live", "Go live", locked=not users_done, reason="" if users_done else "Needs: Two-step login"),
        step("devices", "Your phone", locked=True, reason="Needs: Go live"),
    ]


class Panel(SimpleHTTPRequestHandler):
    """Like the add-on: the page at /, its files under /static."""

    def translate_path(self, path):
        if path.split("?")[0] in ("/", ""):
            path = "/static/index.html"
        return super().translate_path(path)

    def log_message(self, *args):
        pass


@pytest.fixture()
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(Panel, directory=str(STATIC.parent)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    httpd.shutdown()


def test_limiting_the_last_person_to_home_shows_continue_right_away(server):
    fake = {"local_only": False}

    def people():
        return [
            {"id": "me", "name": "Alex", "you": True, "mfa": True, "local_only": False, "owner": True},
            {"id": "kid", "name": "Sam", "you": False, "mfa": False, "local_only": fake["local_only"], "owner": False},
        ]

    def handle(route):
        path = route.request.url.split("/api/", 1)[1]
        if path == "state":
            body = {"settings": {"mode": "hosted", "live": False}, "steps": steps(fake["local_only"]), "complete": False, "landing": "users"}
        elif path.startswith("users/kid/local-only"):
            fake["local_only"] = True
            body = {"users": people(), "blocking": []}
        elif path == "users":
            body = {"users": people(), "blocking": [] if fake["local_only"] else ["Sam"]}
        else:
            body = {"ok": True}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.route("**/api/**", handle)
        page.goto(server + "#users")
        page.get_by_text("Sam", exact=True).wait_for()
        assert page.get_by_role("button", name="Continue: Go live").count() == 0
        page.get_by_role("button", name="Home network only").click()
        page.get_by_role("button", name="Continue: Go live").wait_for(timeout=3000)
        assert page.get_by_role("button", name="Continue: Go live").is_enabled()
        browser.close()
