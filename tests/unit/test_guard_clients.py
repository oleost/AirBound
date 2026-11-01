"""The guard stops logins for apps that aren't allowed (client_id)."""

import pytest
from aiohttp import web
from test_guard import FLOW, fake_ha, form, guarded_client

from airbound.guard import MAX_BLOCKED, OFFICIAL_APPS, AuthGuard, client_name, normalize_client

OWN = "https://home.example.com/"
ALEXA = "https://pitangui.amazon.com/"
EVIL = "https://evil.example/"
HOST = {"Host": "home.example.com"}


async def client_guard(aiohttp_client, steps, allowed=()):
    ha = await aiohttp_client(await fake_ha(steps))
    guard = AuthGuard(str(ha.make_url("")), require_mfa=lambda: True, allowed_clients=lambda: allowed)
    return guard, await aiohttp_client(guard.app())


async def start(client, client_id):
    return await client.post("/auth/login_flow", json={"client_id": client_id}, headers=HOST)


async def test_this_address_may_log_in(aiohttp_client):
    _, client = await client_guard(aiohttp_client, [form("init")])
    assert (await start(client, OWN)).status == 200


async def test_this_address_on_another_port_may_log_in(aiohttp_client):
    _, client = await client_guard(aiohttp_client, [form("init")])
    resp = await client.post("/auth/login_flow", json={"client_id": "https://home.example.com:8443/"}, headers={"Host": "home.example.com:8443"})
    assert resp.status == 200


@pytest.mark.parametrize("app", OFFICIAL_APPS)
async def test_official_apps_may_log_in(aiohttp_client, app):
    _, client = await client_guard(aiohttp_client, [form("init")])
    assert (await start(client, app)).status == 200


async def test_allowed_app_may_log_in(aiohttp_client):
    guard, client = await client_guard(aiohttp_client, [form("init")], allowed=(ALEXA,))
    assert (await start(client, ALEXA)).status == 200
    assert not guard.blocked


async def test_unknown_app_is_stopped_and_listed(aiohttp_client):
    guard, client = await client_guard(aiohttp_client, [])  # no answers: HA is never asked
    resp = await start(client, EVIL)
    assert resp.status == 403
    message = (await resp.json())["message"]
    assert EVIL in message and "Allowed apps" in message
    await start(client, EVIL)
    assert guard.blocked[EVIL]["count"] == 2


async def test_a_later_step_cant_switch_to_an_unknown_app(aiohttp_client):
    _, client = await client_guard(aiohttp_client, [])
    resp = await client.post(f"/auth/login_flow/{FLOW}", json={"client_id": EVIL, "username": "u", "password": "p"}, headers=HOST)
    assert resp.status == 403


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"[]",
        b"{}",
        b'{"client_id": 1}',
        b'{"client_id": "https://home.example.com/", "client_id": "https://evil.example/"}',
        b'{"client_id": "https://evil.example\\\\@home.example.com/"}',
        b'{"client_id": "https://me@home.example.com/"}',
        b'{"client_id": "https://home.example.com/?x=1"}',
        b'{"client_id": "https://home.example.com/\\n"}',
        b'{"client_id": "homeassistant://auth-callback"}',
    ],
)
async def test_unclear_requests_are_stopped_and_not_listed(aiohttp_client, body):
    guard, client = await client_guard(aiohttp_client, [])
    resp = await client.post("/auth/login_flow", data=body, headers={**HOST, "Content-Type": "application/json"})
    assert resp.status == 403
    assert not guard.blocked


async def test_stopped_list_stays_short(aiohttp_client):
    guard, client = await client_guard(aiohttp_client, [])
    for number in range(MAX_BLOCKED + 5):
        await start(client, f"https://site{number}.example/")
    assert len(guard.blocked) == MAX_BLOCKED
    assert f"https://site{MAX_BLOCKED + 4}.example/" in guard.blocked


async def test_any_app_may_log_in_when_turned_off(aiohttp_client):
    _, _, client = await guarded_client(aiohttp_client, [form("init")])
    assert (await client.post("/auth/login_flow", json={"client_id": EVIL})).status == 200


async def test_reading_the_login_methods_is_not_stopped(aiohttp_client):
    async def providers(_request):
        return web.json_response([])

    ha = web.Application()
    ha.router.add_get("/auth/login_flow", providers)
    server = await aiohttp_client(ha)
    guard = AuthGuard(str(server.make_url("")), require_mfa=lambda: True, allowed_clients=lambda: ())
    client = await aiohttp_client(guard.app())
    assert (await client.get("/auth/login_flow")).status == 200


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("HTTPS://Pitangui.Amazon.com", ALEXA),
        ("https://example.com:443/", "https://example.com/"),
        ("http://example.com:80/", "http://example.com/"),
        ("https://example.com:8443/a/B", "https://example.com:8443/a/B"),
        ("https://home-assistant.io/iOS", "https://home-assistant.io/iOS"),
        ("ftp://example.com/", None),
        ("https://", None),
        (None, None),
        ("https://exa mple.com/", None),
    ],
)
def test_normalize_client(value, expected):
    assert normalize_client(value) == expected


def test_client_names():
    assert client_name(ALEXA) == "Alexa (US and Brazil)"
    assert client_name("https://oauth-redirect.googleusercontent.com/r/my-project") == "Google Assistant"
    assert client_name("https://example.com/") == ""
