from airbound.guard import AuthGuard
from airbound.reporting import Reporter, remote_login_message
from airbound.settings import Store

from test_guard import FLOW, entry, fake_ha, form


def test_remember_flags_only_new_addresses(tmp_path, monkeypatch):
    monkeypatch.setattr("airbound.reporting.STATE_FILE", tmp_path / "reporting.json")
    reporter = Reporter(Store(tmp_path / "settings.json"), None, None, None)
    assert reporter.remember("ola", "203.0.113.7")
    assert not reporter.remember("ola", "203.0.113.7")
    assert reporter.remember("kari", "203.0.113.7")


def test_login_message_names_who_and_where():
    message = remote_login_message("ola", "203.0.113.7")
    assert message.startswith("ola logged in from outside for the first time from 203.0.113.7")
    assert remote_login_message("", "1.2.3.4").startswith("Someone")


async def test_guard_reports_login_refusal_and_failure_with_username(aiohttp_client):
    events = []
    steps = [
        form("init"),
        {"type": "form", "flow_id": FLOW, "step_id": "init", "errors": {"base": "invalid_auth"}},
        entry(),
    ]
    ha = await aiohttp_client(await fake_ha(steps))
    guard = AuthGuard(str(ha.make_url("")), require_mfa=lambda: True, listener=lambda e, i: events.append((e, i)))
    client = await aiohttp_client(guard.app())
    headers = {"X-Forwarded-For": "203.0.113.7"}
    await client.post("/auth/login_flow", json={}, headers=headers)
    await client.post(f"/auth/login_flow/{FLOW}", json={"username": "ola", "password": "wrong"}, headers=headers)
    await client.post(f"/auth/login_flow/{FLOW}", json={"username": "ola", "password": "right"}, headers=headers)
    assert [(e, i["ip"], i["username"]) for e, i in events] == [
        ("failed", "203.0.113.7", "ola"),
        ("refused", "203.0.113.7", "ola"),
    ]
    assert events[0][1]["path"] == f"/auth/login_flow/{FLOW}"


async def test_guard_reports_successful_login(aiohttp_client):
    events = []
    ha = await aiohttp_client(await fake_ha([form("mfa"), entry()]))
    guard = AuthGuard(str(ha.make_url("")), require_mfa=lambda: True, listener=lambda e, i: events.append((e, i)))
    client = await aiohttp_client(guard.app())
    await client.post(f"/auth/login_flow/{FLOW}", json={"username": "kari", "password": "p"}, headers={"X-Forwarded-For": "198.51.100.4"})
    await client.post(f"/auth/login_flow/{FLOW}", json={"code": "123456"}, headers={"X-Forwarded-For": "198.51.100.4"})
    assert [(e, i["ip"], i["username"]) for e, i in events] == [("login", "198.51.100.4", "kari")]
