from aiohttp import web

from airbound.guard import FLOW_TTL, REFUSAL_MESSAGE, AuthGuard, FlowTracker

FLOW = "f1"


def form(step_id: str, flow_id: str = FLOW) -> dict:
    return {"type": "form", "flow_id": flow_id, "step_id": step_id}


def entry(flow_id: str = FLOW) -> dict:
    return {"type": "create_entry", "flow_id": flow_id, "result": "code"}


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_login_without_mfa_step_is_refused():
    tracker = FlowTracker()
    assert tracker.allows(form("init"))
    assert not tracker.allows(entry())


def test_login_with_mfa_step_is_allowed():
    tracker = FlowTracker()
    tracker.allows(form("init"))
    assert tracker.allows(form("mfa"))
    assert tracker.allows(entry())


def test_module_selection_counts_as_mfa():
    tracker = FlowTracker()
    tracker.allows(form("select_mfa_module"))
    assert tracker.allows(entry())


def test_mfa_in_one_flow_does_not_unlock_another():
    tracker = FlowTracker()
    tracker.allows(form("mfa", flow_id="other"))
    assert not tracker.allows(entry(FLOW))


def test_flow_can_only_finish_once():
    tracker = FlowTracker()
    tracker.allows(form("mfa"))
    assert tracker.allows(entry())
    assert not tracker.allows(entry())


def test_mfa_record_expires():
    clock = Clock()
    tracker = FlowTracker(clock)
    tracker.allows(form("mfa"))
    clock.now = FLOW_TTL + 1
    assert not tracker.allows(entry())


def test_entry_without_flow_id_is_refused():
    assert not FlowTracker().allows({"type": "create_entry", "result": "code"})


def test_errors_and_non_dict_responses_pass():
    tracker = FlowTracker()
    assert tracker.allows({"type": "form", "flow_id": FLOW, "step_id": "init", "errors": {"base": "invalid_auth"}})
    assert tracker.allows(["not", "a", "flow"])


async def fake_ha(steps: list[dict]) -> web.Application:
    """A stand-in for HA's login flow that returns the given responses in order."""
    remaining = list(steps)
    seen_headers: list[dict] = []

    async def handler(request: web.Request) -> web.Response:
        seen_headers.append(dict(request.headers))
        return web.json_response(remaining.pop(0))

    app = web.Application()
    app.router.add_route("*", "/auth/login_flow", handler)
    app.router.add_route("*", "/auth/login_flow/{flow_id}", handler)
    app["seen_headers"] = seen_headers
    return app


async def guarded_client(aiohttp_client, steps, require_mfa=True):
    ha = await aiohttp_client(await fake_ha(steps))
    guard = AuthGuard(str(ha.make_url("")), require_mfa=lambda: require_mfa)
    return guard, ha, await aiohttp_client(guard.app())


async def test_proxy_refuses_code_without_mfa(aiohttp_client):
    guard, _, client = await guarded_client(aiohttp_client, [form("init"), entry()])
    await client.post("/auth/login_flow", json={})
    resp = await client.post(f"/auth/login_flow/{FLOW}", json={"username": "u", "password": "p"})
    assert resp.status == 403
    assert await resp.json() == {"message": REFUSAL_MESSAGE}
    assert guard.refused == 1


async def test_proxy_passes_code_after_mfa(aiohttp_client):
    _, _, client = await guarded_client(aiohttp_client, [form("init"), form("mfa"), entry()])
    await client.post("/auth/login_flow", json={})
    await client.post(f"/auth/login_flow/{FLOW}", json={"username": "u", "password": "p"})
    resp = await client.post(f"/auth/login_flow/{FLOW}", json={"code": "123456"})
    assert (await resp.json())["result"] == "code"


async def test_enforcement_can_be_turned_off(aiohttp_client):
    _, _, client = await guarded_client(aiohttp_client, [entry()], require_mfa=False)
    resp = await client.post(f"/auth/login_flow/{FLOW}", json={})
    assert (await resp.json())["type"] == "create_entry"


async def test_forwarded_for_reaches_ha(aiohttp_client):
    _, ha, client = await guarded_client(aiohttp_client, [form("init")])
    await client.post("/auth/login_flow", json={}, headers={"X-Forwarded-For": "203.0.113.7"})
    assert ha.server.app["seen_headers"][0]["X-Forwarded-For"] == "203.0.113.7"
