"""The headers every answer from the panel carries."""

from aiohttp import web

from airbound import web as panel


async def test_every_answer_carries_the_policy_even_a_refusal(aiohttp_client, monkeypatch):
    async def page(_request):
        return web.Response(text="ok")

    app = web.Application(middlewares=[panel.security_headers, panel.ingress_only])
    app.router.add_get("/", page)
    client = await aiohttp_client(app)
    refused = await client.get("/")  # the test client isn't Home Assistant's ingress
    assert refused.status == 403
    monkeypatch.setattr(panel, "INGRESS_PROXY", "127.0.0.1")
    served = await client.get("/")
    assert served.status == 200
    for response in (refused, served):
        policy = response.headers["Content-Security-Policy"]
        assert "default-src 'none'" in policy and "script-src 'self';" in policy
        assert "'unsafe-inline'" not in policy.split("script-src")[1].split(";")[0]
        assert "'unsafe-eval'" not in policy
        assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_the_page_has_nothing_the_policy_would_stop():
    page = (panel.STATIC / "index.html").read_text(encoding="utf-8")
    script = (panel.STATIC / "app.js").read_text(encoding="utf-8")
    assert "<script>" not in page and "<style" not in page
    for handler in (" onclick=", " onerror=", " onload=", " onchange=", "javascript:", "eval(", "new Function"):
        assert handler not in page and handler not in script, handler
