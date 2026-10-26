"""The API behind the setup wizard, through HA ingress."""

from __future__ import annotations

import secrets

from conftest import VM_HOST, create_user
from ha import HA


async def test_state_never_reveals_the_token(panel, live):
    state = await panel.get("api/state")
    assert "cloudflare_token" not in state["settings"]
    assert state["steps"][0]["id"] == "cloudflare"
    assert {"done", "locked", "reason", "summary", "title"} <= set(state["steps"][0])
    assert state["landing"]


async def test_bad_cloudflare_token_gets_a_plain_answer(panel):
    result = await panel.post("api/cloudflare", {"token": "not-a-real-token"}, expect=400)
    assert "Cloudflare didn't accept this token" in result["error"]


async def test_empty_cloudflare_token(panel):
    result = await panel.post("api/cloudflare", {"token": "  "}, expect=400)
    assert result["error"] == "Paste the token first."


async def test_ha_settings_are_done_and_verified(panel):
    status = await panel.get("api/ha-settings")
    assert status["changes"] == []
    assert status["proxy_trusted"] is True
    assert status["pending"] is False


async def test_users_shows_who_is_looking(panel):
    async with HA() as ha:
        current = await ha.call({"type": "auth/current_user"})
    result = await panel.get("api/users")
    me = [u for u in result["users"] if u["you"]]
    assert len(me) == 1 and me[0]["id"] == current["id"]


async def test_limit_user_to_home_network(panel):
    async with HA() as ha:
        uid = await create_user(ha, f"abtest_local_{secrets.token_hex(3)}", secrets.token_urlsafe(16))
    try:
        before = await panel.get("api/users")
        assert any(u["id"] == uid for u in before["users"] if not u["mfa"] and not u["local_only"])
        after = await panel.post(f"api/users/{uid}/local-only", {"local_only": True})
        person = next(u for u in after["users"] if u["id"] == uid)
        assert person["local_only"] is True
        assert person["name"] not in after["blocking"]
    finally:
        async with HA() as ha:
            await ha.call({"type": "config/auth/delete", "user_id": uid})


async def test_router_values(panel):
    info = await panel.get("api/router")
    assert info["ha_ip"] == VM_HOST
    assert info["ha_port"] == 80
    assert info["public_ip"]


async def test_status_page_checks(panel, live):
    status = await panel.get("api/status")
    by_id = {c["id"]: c for c in status["checks"]}
    assert by_id["http"]["ok"]
    assert by_id["certificate"]["ok"]
    assert by_id["trusted_networks"]["ok"]
    assert status["proxy_running"] is True
    for check in status["checks"]:
        if not check["ok"]:
            assert check["detail"], check


async def test_qr_code(panel, live):
    async with panel.session.get(f"{panel.base}api/qr.svg") as resp:
        assert resp.status == 200
        assert "<svg" in await resp.text()


async def test_panel_is_only_reachable_through_ingress():
    import aiohttp

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(f"http://{VM_HOST}:8099/api/state", timeout=aiohttp.ClientTimeout(total=3)) as resp:
                assert resp.status == 403
        except aiohttp.ClientError:
            pass  # not even exposed: also fine
