"""End-to-end checks of remote access through AirBound on the test VM."""

from __future__ import annotations

import aiohttp
import pytest

from conftest import EXTERNAL_URL, LOCAL_URL, LoginFlow, my_ip_towards_vm
from ha import HA


async def test_login_without_mfa_is_refused_remotely(external, users):
    account = users["nomfa"]
    result = await LoginFlow(external, EXTERNAL_URL).login(account["username"], account["password"])
    assert result["status"] == 403
    assert "two-step verification" in result["message"]
    assert "result" not in result


async def test_login_without_mfa_still_works_at_home(local, users):
    account = users["nomfa"]
    result = await LoginFlow(local, LOCAL_URL).login(account["username"], account["password"])
    assert result["type"] == "create_entry"


async def test_login_with_mfa_works_remotely_and_ha_sees_real_ip(external, users):
    account = users["mfa"]
    flow = LoginFlow(external, EXTERNAL_URL)
    result = await flow.login(account["username"], account["password"], account["totp"])
    assert result["type"] == "create_entry", result
    tokens = await flow.token(result["result"])

    # The API works through the proxy with the new token.
    async with external.get(
        f"{EXTERNAL_URL}/api/", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    ) as resp:
        assert resp.status == 200

    # R13: HA recorded the client's real address, not the add-on's.
    async with external.ws_connect(f"{EXTERNAL_URL.replace('https', 'wss')}/api/websocket") as ws:
        await ws.receive_json()
        await ws.send_json({"type": "auth", "access_token": tokens["access_token"]})
        assert (await ws.receive_json())["type"] == "auth_ok"
        await ws.send_json({"id": 1, "type": "auth/refresh_tokens"})
        refresh_tokens = (await ws.receive_json())["result"]
    ips = {t["last_used_ip"] for t in refresh_tokens if t["last_used_ip"]}
    assert ips == {my_ip_towards_vm()}, ips


async def test_unknown_hostname_is_dropped(external):
    with pytest.raises(aiohttp.ClientError):
        async with external.get("https://not-the-hostname.airbound.test/api/"):
            pass


async def test_ha_http_settings_are_hardened():
    async with HA() as ha:
        stable = (await ha.call({"type": "http/config"}))["stable"]
    assert stable["use_x_forwarded_for"] is True
    assert stable["trusted_proxies"] == ["172.30.33.0/24"]
    assert stable["login_attempts_threshold"] > 0
